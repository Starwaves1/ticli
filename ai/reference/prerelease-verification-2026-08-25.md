# Pre-release verification — 2026-08-25

Final review before (a) publishing to Homebrew and (b) merging the fork to
upstream main. Seven area reviewers, a refutation pass, and a completeness
critic ran first; this document is the last reader's verification of their
output against the code, plus the hand-check list Garrett asked for.

What was run: static reading of the fork at e928573, the offline suite
(1,488 tests, loopback only, green with mpv and ffmpeg installed so
nothing skipped), a release rehearsal that built the wheel and installed
it clean, and deterministic repro scripts in scratch space. What was not
and could not be: any live TIDAL request, any audible output, anything
macOS-only, a real keychain, a real terminal emulator. Every claim about
what TIDAL actually serves and what the media keys actually do is
reasoning, not observation — the hand-check list below exists because of
that ceiling.

Seven claims were re-verified independently after the report was drafted,
because they carry the verdict: PyPI really does hold tidal-cli 1.0.2 from
2026-03-12; setuptools 75.8.0 really does reject `license = "MIT"` with a
schema error; the wheel really is 55% test code (708,572 of 1,273,069
bytes); `__init__.py` really says 1.0.0 against pyproject's 1.0.2;
`_next_track` really has no brake and `_play_track` really issues
`_stream_description` before its generation check; `_play_url_locked`
really falls back to `_cached_audio_path` with no tier test. And the
conftest cache-rail leak reproduced itself unbidden — three full-suite
runs during this review left a synthetic 24-play tracker in the reviewer's
own `~/.cache/ticli/audio.json`. On Garrett's machine those three runs
would have been three overwrites of his real play history.

Verification status: I personally re-read the code for every finding named
in "must fix" and most of "should fix" (marked below), and empirically
re-ran the conftest cache-rail leak. Findings marked (relayed) rest on the
refuter's repro scripts, which I read but did not re-run; all of those
repros are in scratch space and are deterministic.

---

## The verdict

**Homebrew: do not publish yet.** Seven fixes first, listed below. Three
are release plumbing (the release as cut today literally ships the March
build), and four are behavior a stranger would hit in the first hour: the
quality-tier feature is inert in its headline case, two held keys fan out
into the request pattern that caused INCIDENTS #1, and a transient network
error at launch silently downgrades a PKCE login to AAC and destroys the
record. None is a large change; the largest is a few dozen lines.

**Merge to upstream main: fine, with caveats.** The fork is a large,
genuine improvement over base and the bugs travel with it either way —
merging changes nothing about them. Merge, then fix the list on main
before tagging anything. The one thing to do in the same breath as the
merge is the conftest cache rail (item 7): from the moment this is main,
anyone who runs the suite on a machine with a real cache overwrites their
own play-count tracker.

---

## What must be fixed before publishing

Ordered by what actually hurts. All personally verified in the code unless
marked otherwise.

**1. The release ships nothing, or ships March.** `src/pyproject.toml:7`
and `src/setup.py:7` still say 1.0.2, and tidal-cli 1.0.2 has been on PyPI
since 2026-03-12 as the pre-fork build (verified against PyPI's JSON by
the release reviewer; version fields verified here). publish.yml has no
`skip-existing`, so the upload 400s while the GitHub Release looks green —
and until then, `pip install tidal-cli` and any formula pinned to the PyPI
sdist deliver the old player with none of what the README advertises.
While in there: `requires = ["setuptools>=64", ...]` at pyproject.toml:2
cannot build `license = "MIT"` (PEP 639 needs >=77 — reproduced with
75.8.0), and `ticli/__init__.py:3` says 1.0.0, a third divergent version.
Fix: bump the version in pyproject (delete or align setup.py and
`__version__`), floor setuptools at 77.

**2. Holding → fires one playbackinfo request per key repeat.**
`_next_track` (player.py:3326) has no brake of any kind — no interval, no
single-flight — and `_play_track`'s worker reaches `_stream_description`
(3072) before the generation check (3075), so every repeat's request is
already on the wire when it is discarded. Measured offline: 25 held
presses, 25 `get_stream()` calls in 0.74s — ~34/s against INCIDENTS #1's
~19/s. Every other repeatable key in the app has a brake; this one is the
door to the exact block that stopped the owner's music. Fix: move the
`_play_gen != gen` check above `_stream_description` and floor skips on a
monotonic interval the way `_start_track_radio` (3505) already does.

**3. Holding l fires one favorites *write* per key repeat.** `_toggle_like`
(player.py:3464) has the same missing brake on an account-mutating
endpoint: each repeat spawns a thread that reads the same `_liked_ids` and
issues `add_track`/`remove_track` — the critic measured 30 writes for one
held key, and with real latency 8 taps produce 8 identical `add_track`
calls because every in-flight thread saw the same set. `_liked_ids` is
also a set mutated in place (`.add`/`.discard`) from N threads and read
bare by the paint at 3833 — the one shape WORKING-RULES says must be
locked (the `_reclaim_deferred` precedent). Fix: an in-flight flag plus
`KEY_REPEAT_WINDOW`, and lock or whole-replace the set.

**4. The re-fetch-at-higher-quality feature is inert.** `_play_url_locked`
(player.py:1646) falls back to `_cached_audio_path(cache_key)` with no
tier check, so the below-tier file `_local_source` (3119) deliberately
refused is found again by stem and played — the freshly paid MAX stream
URL is discarded, and because `have_kept` is then true, the MAX copy is
never downloaded either. The user hears the old HIGH file while the badge
falls back to the setting's label (3888), i.e. the screen says MAX over a
HIGH file, and one playbackinfo request per play is burned for nothing.
The suite's four tests for this stop at a recording fake and never reach
`_play_url_locked` — INCIDENTS #2's shape. Fix: `_play_track` already
knows `_local_source` refused; pass that decision into `play_url` (e.g. an
`allow_cached` flag) so line 1646 is skipped and `_start_download` runs.
**Warning when fixing:** the refuted ".part collision between `[R]` and
the playback downloader" finding (player.py:1425 vs 7647 — same
`{track_id}.part`, opened "wb" by both) is only unreachable *because* of
this bug. Fixing this re-arms it. Close both at once: skip the currently
playing track in `_refetch_candidates`, or share the generation.

**5. A download below the player's tier poisons the entitlement gate.**
`_download_stream_url` (player.py:7782) moves `session.audio_quality` to
the download's tier and calls `_stream_description`, whose
`_note_granted_quality` (3287) compares the grant against the *player's*
setting. Download at MEDIUM while set to MAX, get granted HIGH, and
`_quality_ceiling` becomes HIGH: the settings page invents "this login
isn't served MAX", `[R]` refuses to upgrade, and `_tier_is_enough`
(3191) short-circuits true so every cached copy — 96k LOW included — plays
instead of re-fetching. Self-sustaining on a cached library, since cache
hits describe no stream. `_stream_at_best_tier` (7737) walks down the
ladder on its own, so this fires without the user ever touching the tier
picker. Reproduced offline end to end. Fix: pass the tier actually
requested into `_stream_description`/`_note_granted_quality` and compare
against that, or skip the note while the session tier is overridden.

**6. A failed session restore silently downgrades PKCE to device flow and
overwrites the record.** `_login` (player.py:2446-2484) reads
`is_pkce` from the stored record only to hand it to `load_oauth_session`;
when the restore raises (a 429, a 401 subStatus 4006, wifi not up yet) the
broad except at 2464 swallows it and the flow choice at 2471 comes from
`self._login_flow` — the CLI flag, default "device". The user is asked to
log in for no visible reason, and `_finish_login` → `_save_session`
(2505) writes `is_pkce: False` over the PKCE record. From then on every
track is 320k AAC while the status line goes on printing the setting's
FLAC label. This is the exact "silent drop back to AAC" the comment at
2467 says the design exists to remove. Fix: when the stored record says
`is_pkce`, re-login via PKCE (or at minimum refuse to overwrite the
record from a device-flow login without asking).

**7. A full cache can never take a new song, and running the test suite
destroys the real tracker.** Two halves, same subsystem:

- `should_cache` (cache.py:822) values the candidate with `playing=True`;
  `enforce_budget` (cache.py:967) values the same file moments later
  without it. A freshly cached track scores `(0, now)`, sorts below every
  one-play resident, and is deleted by the sweep its own download
  triggers. Net behavior at budget: every first play downloads the whole
  track and throws it away; only a second play sticks — which
  DECISIONS.md explicitly rejects as "the freeze in another costume", and
  which contradicts DECISIONS.md:126's stated rule that the playing song
  "counts the play it is earning right now". Reproduced with the real
  MetadataCache. Fix: let the sweep see the play being earned — count it
  in `note_cached`, or pass the active track into `enforce_budget`.
- `tests/conftest.py` rails `DOWNLOAD_ROOT` and `STATE_DIR` but not
  `cache.CACHE_DIR`. Verified here: running `test_player_controls.py`
  alone, with HOME pointed at an empty directory, leaves
  `~/.cache/ticli/audio.json` holding a one-entry synthetic tracker — a
  monitor daemon thread outlives its test's monkeypatch and calls
  `note_played` after teardown restored the real path. On the owner's
  machine every suite run replaces his real play history; `reconcile()`
  then re-adopts his songs at zero plays and eviction order is reset.
  This is the WORKING-RULES hard rule ("tests must not touch the owner's
  real cache directory") breached by the rails file itself, INCIDENTS #6
  one directory over. Fix: a third autouse fixture redirecting
  `cache_mod.CACHE_DIR` (and `ART_DIR`).

## What should be fixed, soon but not gating

- **'²' on a settings int row crashes ticli** (player.py:8675/8688 gate on
  `isdigit()`, 8006 parses with `int()`; superscript and enclosed digits
  pass the first and fail the second). Deterministic, and on AZERTY the ²
  key sits left of 1. Verified in code; crash repro relayed. Fix:
  `isdecimal()` in both places, or try/except the int.
- **A quality change during a bulk download is silently reverted on the
  session** (player.py:7789 restores a pre-captured value over
  `_apply_setting`'s write; nothing re-syncs until the setting is touched
  again, and the next grant then poisons the gate as in item 5). Window is
  one full API round trip per downloaded track. (relayed, repro read)
- **`_monitor_playback` has no exception guard** (player.py:3585): one
  escaping exception silently ends auto-advance, the 10s autosave, seek
  delivery and media keys for the session. Demonstrated triggers are
  narrow races (`_maybe_prefetch_next`'s two-step queue read at 3219-3224,
  `_get_position`'s double read of `_play_start_time`), but the cost of
  the guard is one try/except and the failure mode is invisible.
- **A track start in flight at quit spawns mpv after `_shutdown`**
  (player.py:3075 checks `_play_gen`/`_playing`, never `self.running`;
  nothing in the quit path bumps the generation). The orphan outlives the
  app — INCIDENTS #7's symptom by another door. Also reachable by
  quitting in the gap between two songs (auto-advance). (verified shape;
  repro relayed)
- **FLAC downloads claim "cover art" that was never written**
  (utils/tags.py:329 — `_tag_flac` never reads its `cover` argument;
  `write_tags`:413 still returns `describe(meta, cover)` which appends
  "cover art"). The LOSSLESS tier on the PKCE login is exactly the path
  that produces FLAC. `describe`'s docstring promises the interface "can
  never claim a tag that was not written"; this is the one case it does.
  Fix: write the PICTURE block, or stop claiming it.
- **`MAX_COMPONENT = 90` counts characters, filesystems count bytes**
  (utils/downloads.py:190; the comment at 111 claims UTF-8 safety, but 90
  CJK characters are 270 bytes against NAME_MAX 255, so `os.replace` in
  `_download_deliver` fails with ENAMETOOLONG). One or two tracks of a
  multilingual batch fail forever with a generic error. Fix: truncate by
  encoded byte length.
- **Two tracks can resolve to one download path** (utils/downloads.py:244
  — no disc number, no uniqueness suffix; disc 2 track 01 lands on disc 1
  track 01's file, playing A then plays B's audio, and deleting either
  row unlinks the shared file). Realistic trigger is a multi-disc release.
  The " (2)" suffix that MAX_COMPONENT's comment budgets for was never
  implemented. (relayed, code read)
- **Enter on the search screen has no repeat brake** (player.py:6074 —
  `_do_search` destroys `_search_fetching` via `_reset_search_results`
  before `_apply_search_scope` checks it). Enter-as-refresh is pinned by a
  test and should survive; floor it on `SEARCH_FETCH_MIN_INTERVAL` the
  way `_search_more` (6197) does. (relayed)
- **The fallback token file is written non-atomically**
  (credential_store.py:76 — `write_text` truncates in place; every other
  JSON writer in the project is temp+rename). ENOSPC or power loss
  mid-write loses the tokens, and via item 6 that currently also costs a
  PKCE user his flow. Three-line fix, same pattern as next door.
- **A [u] upgrade that fails inside `process_auth_token` leaves PKCE
  tokens flagged `is_pkce=False` in the live session** (player.py:2579;
  tidalapi assigns tokens before the flag, with two network calls in
  between). Refresh then goes to the device client and dies hours later —
  the documented "random logout". In-memory only; restart clears it.
  (relayed, repro against real tidalapi read)
- **A non-interactive `ticli` performs the full device login poll before
  the tty check** (player.py:8954 — isatty is tested after `_login`, so a
  piped run or a formula test block polls TIDAL's token endpoint for the
  device code's full ~300s, then refuses). Pre-existing upstream, but fix
  before Homebrew: move the check to the top of `run()`. Ctrl-C during a
  real login also appears dead for the same reason (non-daemon executor
  in tidalapi). (relayed)
- **A malformed JPEG can cost ~0.5GB and seconds of CPU per component
  before validation** (utils/artwork.py:382-388 allocates planes from the
  SOF's unvalidated 16-bit dimensions). Nothing crashes — `decode()`
  catches — but a truncated CDN response burns a laptop first. One `w*h`
  ceiling before the allocation. (code verified, measurement relayed)
- Small honesty/hygiene items: the prefetched URL is not dropped on a
  quality change (player.py:8028 — one track plays the old tier under the
  new label); display builders re-read `self._queue` per row instead of
  snapshotting once (player.py:4255 — a paint racing a radio swap can
  IndexError; ~1e-4 odds, one-line fix); the late-download generation
  re-check can blank the successor's `_cache_file` (player.py:1459);
  startup refusals exit 0 (player.py:8897/8903/8956); API-supplied names
  reach the terminal unfiltered for ESC (player.py:3876 — no defense if
  TIDAL ever passes one through; one-line strip in the row builders);
  `/tmp/ticli-player-<pid>.log` is 0644 and never unlinked on Linux
  (player.py:1498 — backend stderr can hold signed CDN URLs);
  `run.add(items)`'s refusal is discarded (player.py:7134 — residual
  window is documented and microseconds wide, but the returned False is
  the one signal production never reads).

---

## What to watch for when you run it by hand

Ranked: the ones that would be most embarrassing in public first. Each is
do X -> right looks like Y -> wrong looks like Z. Where a bug above is
unfixed, the "wrong" described is what you will currently see.

1. **Quality tiers round trip.** With cache on, play a track at HIGH, quit,
   set MAX, play it again -> right: a short network fetch, then the badge
   says MAX and the cache record for that id says HI_RES_LOSSLESS ->
   wrong: instant start (no fetch) with MAX on the status line — that is
   item 4, the old HIGH file under a MAX label. Check the record with:
   `python3 -c "import json,pathlib;print(json.load(open(pathlib.Path.home()/'.cache/ticli/audio.json'))['tracks']['<id>'])"`.
2. **The settings page never claims you lack an entitlement you have.**
   Download one track at MEDIUM from the [d] box, then open settings ->
   right: no gate note -> wrong: "HIGH and MAX — this login isn't served
   them ... [u] fixes it" (item 5). If it appears, restart clears it.
3. **Skip and like, gently, then hold.** Tap → three times fast -> right:
   the third track plays, no error toast. Do NOT hold → or l down for a
   full second until items 2 and 3 are fixed — on a 25+ track queue that
   is a 30+/s request burst at the endpoint family from INCIDENTS #1 and
   it can stop your music for minutes. After the brakes land, hold → for
   one second -> right: a handful of skips and roughly one request per
   landed track -> wrong: a 429/4006 toast, or search and playback dying
   together for 60-90s.
4. **PKCE survives a bad launch.** With a PKCE session saved, turn wifi
   off, run `ticli` -> right: an offline/failed message that does NOT
   offer a fresh device login, and afterwards the record still has
   `"is_pkce": true` (keychain entry or `~/.config/ticli/session.json`)
   -> wrong: "Open this URL to login" (item 6 — if you complete it, your
   FLAC is silently gone until you notice the settings page changed).
   Repeat once on hotel-grade wifi; a 429 at launch is the same door.
5. **The cache still grows at its budget.** Set the budget low enough to
   fill (or fill it), then play a brand-new track and watch settings ->
   right: song count rises by one and an old song leaves -> wrong: the
   count never moves and the new track re-downloads on every play (item
   7a). Also `ls` the audio dir after: the file for the track you just
   played should exist.
6. **macOS media keys and Now Playing.** Play, press ⏯ ⏭ ⏮ from the
   keyboard and from AirPods -> right: they act on ticli, and Control
   Center / lock screen shows "Track — Artist" -> wrong: nothing happens,
   or the title shown is a long signed CDN URL. Then disconnect the
   AirPods mid-track -> right: playback pauses or fails *visibly* with a
   toast -> wrong: the queue silently burns through tracks (INCIDENTS
   #3's shape). Also watch for a rhythmic ~0.5s stutter in the UI clock —
   that is the media-key bind retrying with a socket timeout on the
   monitor thread.
7. **Silence is the enemy.** Play ten tracks of your real library on mpv,
   then force ffplay (`PATH` without mpv) and play three more -> right:
   audible audio on both, seek and pause included -> wrong: the UI
   advancing normally with no sound — if you ever see that, check
   `/tmp/ticli-player-*.log` first. This review ran zero seconds of real
   audio; the whole DASH->HLS path is unverified here.
8. **Quit leaves nothing behind.** While a track is starting (the
   half-second after pressing Enter on a search result), Esc-Esc out ->
   right: silence, and `pgrep -af 'mpv|ffplay'` is empty -> wrong: music
   continuing after the prompt returns (the shutdown-window orphan).
   Repeat once by quitting exactly as a track ends.
9. **Multi-disc download.** Download a 2+ disc album ([d] on the album)
   -> right: every track present under ~/Music/Ticli, disc 2's files
   distinct from disc 1's -> wrong: fewer files than tracks, or deleting
   one row in the downloads list making a different song vanish. While
   there: download something Japanese or emoji-heavy with a long title ->
   right: it lands -> wrong: one track failing forever with a generic
   error (the 255-byte cap).
10. **FLAC tagging honesty.** Download a track on the PKCE login that
    arrives as .flac, open it in anything that shows artwork -> right:
    cover shown, or ticli's toast did not claim "cover art" -> wrong:
    toast said "Tagged: ... cover art" and the file has none.
11. **Keychain, first unlock, and stderr.** First run on the Mac: tokens
    should land in the keychain (`security find-generic-password -s
    ticli` or similar), and a later locked-keychain launch should degrade
    politely -> wrong: a log line wedged mid-screen into the player pane
    (nothing configures logging; warnings write to the alternate screen
    under Rich).
12. **Resize torture.** Drag the window slowly across ~83 columns and
    smaller, with artwork on, in Terminal.app (16-color), iTerm2, and
    tmux -> right: layout reflows, artwork appears only where the
    terminal can show it, and each size settles -> wrong: stranded rows,
    color bleeding, or the cover shrinking as the window grows. Searches
    whose results contain odd punctuation should never damage the frame.
13. **Slow network.** Throttle to ~1 Mbps, play something uncached ->
    right: buffering that eventually plays, downloads box shows honest
    rates -> wrong: a cover that never appears for the whole track (the
    stored-None no-retry), or the UI clock stalling in step with IPC
    timeouts.
14. **Settings input.** On "Songs per page" type 15, Enter; then type ²
    (AltGr+2 / option-00B2), Enter -> right: the row reverts or clamps ->
    wrong: ticli exits with a ValueError traceback (unfixed item).
15. **Piped/scripted runs.** `echo q | ticli` -> right: an immediate
    "requires an interactive terminal" and no login URL -> wrong: a login
    URL and a five-minute hang (the isatty ordering). `ticli; echo $?`
    after a refusal currently prints 0 — remember that when writing any
    wrapper.

## Homebrew-specific notes

- Do not pin the PyPI sdist until the version is bumped — 1.0.2 on PyPI
  is the pre-fork March build. A formula built from the GitHub tarball
  gets the root LICENSE; the PyPI artifacts contain no license text at
  all until one is copied into src/.
- Dependencies: python 3.10+; mpv is the recommended runtime dep (ffplay
  via ffmpeg is a working fallback — src/README.md still wrongly says mpv
  is required). The `keyring` extra is optional; without it tokens live
  in a chmod-600 file. termios means macOS/Linux only.
- The build backend floor is wrong (`setuptools>=64` cannot process
  `license = "MIT"`); any build that vendors setuptools <77 or passes
  `build_isolation: false` dies with a misleading schema error. Fix the
  floor rather than working around it in the formula.
- The wheel currently ships the entire test suite (55% of the payload)
  minus its fixtures; add `exclude = ["ticli.tests*"]`.
- The `test do` block: do NOT invoke bare `ticli` — with no saved session
  it starts a real device-code poll against TIDAL for ~5 minutes before
  noticing there is no tty. Use `ticli --help` (works today). A
  `--version` flag does not exist and `ticli.__version__` says 1.0.0;
  add `click.version_option` if the conventional assert is wanted. All
  startup refusals currently exit 0.

## Looked at and found nothing wrong with

- The MP4 tagger's byte surgery: verified against real ffmpeg-generated
  files including fragmented MP4 and FLAC-in-MP4 — audio stream md5
  unchanged, covers embedded, ffprobe agrees. The stco/co64 shifting is
  correct.
- The reap-and-respawn critical section in `_play_url_locked` — the
  INCIDENTS #7 fix holds, including `resume()`.
- Destructive-operation discipline: clear paths enumerate owned names
  only, the decoy-file test stands, download deletion confirms first,
  `[R]` cannot downgrade (`_is_upgrade` is strictly-greater).
- The dependency closure: `requests` is a hard tidalapi dependency and
  its absence cannot produce the misattributed error (refutation
  verified).
- The download-run handoff design: `_go`'s clear-then-`pending()` order
  covers every interleave except a microsecond window the code documents
  and re-queues around; the finder's wide-window claim was wrong.
- Atomic write discipline in config.py, state, and the cache tracker.
- The offline suite itself, and — after mpv 0.37.0 and ffmpeg 6.1.1 were
  installed into the review container — the eight real-backend tests that
  skip on a bare machine. **1,488 passed, 0 skipped, 0 failed.** That
  covers `test_buffering.py` in full: mpv and ffplay each played ticli's
  own hand-built HLS playlist, with real fragmented-FLAC segments served
  over a real socket, and the readahead measurements (`-infbuf`,
  `--cache-secs`) reproduced the numbers the docstrings claim. Two of
  those eight first failed for a sandbox reason worth writing down, not a
  code one: this container has no `/dev/snd`, so mpv exits 2 on audio-device
  init and ffplay's read thread never advances. Under `ao=null` /
  `SDL_AUDIODRIVER=dummy` all eight pass. Anyone re-running these on a
  headless box needs that, or they will file two bugs that do not exist.

## What this review could not check

Live TIDAL behavior of any kind: what tiers this login is actually
granted, real 429/4006 behavior, DASH manifests, radio quality, whether
hostile characters can appear in real playlist names. Audible output on
either backend — the segment machinery of the hand-built HLS path *was*
exercised against real mpv and ffplay (see the green list above), but
every backend in this review ran into a null audio device, so "does sound
come out, and is it the right sound" remains the single most important
thing to verify by ear, against TIDAL's own segments rather than
ffmpeg-generated ones. Everything macOS: media keys, Now
Playing, the keychain, `/var/folders` temp semantics. Real terminal
emulators and reflow. The `[R]` re-fetch job against a real library. This
review earns moderate confidence on the paths it executed and explicitly
none on those; the hand-check list is the other half of the review.
