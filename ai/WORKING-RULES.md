# Working rules

Constraints that hold across the project. Most were learned the expensive way;
the ones marked **hard** have already caused real damage when violated.

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

Practical consequences that shaped features:
- Build against fakes. Read the installed `tidalapi` source
  (`~/.local/pipx/venvs/tidal-cli/lib/python3.14/site-packages/tidalapi/`)
  instead of probing. Several agents completed substantial features with
  **zero** live requests.
- Runtime behavior must be frugal too. Any key a user might hold down must not
  fan out into requests. Existing brakes: single-flight (`_search_fetching`),
  a minimum interval (`SEARCH_FETCH_MIN_INTERVAL`), a generation counter that
  discards late-landing results (`_search_gen`), and a hard offset ceiling
  (`SEARCH_MAX_OFFSET = 300`, tidalapi's documented max).
- Signed stream URLs expire in ~1 hour. Fetch a track's URL immediately before
  using it, never all up front for a batch.
- `RequestSession.basic_request` auto-refreshes an expired token **only** when
  the response body says `"The token has expired."` It will not auto-refresh a
  4006. A long-running job must tolerate a mid-run refresh.

## Dependencies

**No new Python dependencies.** This has been held throughout and has produced
better solutions rather than worse ones — most notably a stdlib-only JPEG
decoder (see HISTORY, artwork) that turned out to be both feasible and fast.

`ffmpeg` is an *implicit* dependency via ffplay, but mpv users may not have it.
Anything using ffmpeg must degrade gracefully when it is absent — never fail
an operation because ffmpeg is missing. The caching path was deliberately
rewritten to remove ffmpeg entirely.

## Threading and concurrency

- **Network never on the UI thread.** Blocking the UI thread was a measured
  cause of input lag; `get_url()` on the UI thread froze the interface on
  every track start.
- **Daemon threads only.**
- **Reads of whole-object-replaced state are lock-free. GIL-reliant.**
  Assign whole objects; a reader then sees some generation of the object (or
  of a whole-replaced file), never a torn one, and needs no lock. State that
  *must* be mutated in place is the other case, and it is locked on every
  touch, reads included — the precedent is the reclaim set
  (`_reclaim_deferred` under `_reclaim_lock`, 2026-07-27): a set added to
  and discarded from by several threads cannot be whole-replaced without
  losing another thread's element, so nothing about it is safe to read bare.
  Prefer whole-object replacement wherever it fits; when it cannot, don't
  half-lock.
- **A multi-writer load-modify-save cycle over one on-disk JSON file is the
  exception: it gets a leaf lock.** Whole-object assignment cannot stop two
  threads' read-edit-write cycles over the same file from erasing each
  other, so the *whole* cycle — read included, since the stale read is what
  loses the other writer's update — is held behind one `threading.Lock`.
  The lock is a leaf: held only across the cycle, taken exactly once, and
  nothing inside it may call another lock-taking function (plain `Lock` is
  not reentrant — restructure into unlocked `_locked` halves instead).
  Precedents: the cache tracker's `_tracker_lock` (2026-07-27), the player
  state's `_state_lock` (2026-08-02), the download index's `_index_lock`
  (2026-08-02). Cross-*process* races over these files are no longer left to
  chance either, but the answer is not a lock per file: since 2026-08-07 an
  advisory `flock` in `run()` means only one ticli runs at a time
  (`_take_instance_lock`), so the second writer no longer exists. That guard
  is best-effort by design — a filesystem that cannot take a lock lets the
  run proceed — so these leaf locks remain load-bearing and none of them may
  be removed on the strength of it.
- **Hard: replacing an external resource is one critical section, not two.**
  Killing the old audio process and spawning the new one were two separate
  acquisitions of the same lock, and the instant between them let a second
  starter spawn a backend of its own and orphan the first — two songs playing
  at once, the loser unreachable because `_process` is the only handle
  anything uses (INCIDENTS #7). The general shape: when a single slot holds
  the only reference to something that must be destroyed before its
  replacement is created, the destroy and the create are one hold of the lock.
  Releasing in between is a window in which the slot is empty and a spawn is
  still owed, so a second caller finds nothing to clean up. Precedents:
  `_play_url_locked` (2026-08-07) and `seek_to`, which always had it right.
  A generation counter does not substitute — the two racing callers each
  carried a currently-valid generation.
- **Never hand the lock back by hand.** `resume()` did
  `self._lock.release()` … call … `self._lock.acquire()` to reach a
  lock-taking method; that is the same window as above, wearing a disguise.
  A plain `Lock` is not reentrant, and the answer to that is always an
  unlocked `_locked` half, never an unlock-and-hope.
- Generation counters are the established pattern for "a result landed after
  the thing it was for went away" (`_search_gen`, `_play_gen`,
  `_download_gen`, `_artwork_request`).

## Power and wakeups

**No new polling loops or timers.** The monitor thread's existing 0.5s tick is
the ceiling; piggyback on it. "Roughly zero power" is an explicit product goal,
not a nicety.

Achieved state worth not regressing: idle terminal traffic is **zero** (the
repaint is skipped when the render is byte-identical), and the input loop is
woken by a self-pipe when background work lands rather than by polling.

## Rendering

- Rich `Live` runs with `auto_refresh=False` and `screen=True`. Repaint on
  demand via `_repaint()`. The `auto_refresh=False` choice is what took
  keypress-to-repaint from 231ms to 1.1ms — do not reintroduce a refresh
  thread without a measured reason.
- A `SIGWINCH` handler forces a repaint on resize.
- `_repaint`'s skip-if-identical cache is keyed on `console.size` **as well as**
  the rendered segments, because a window that only got shorter renders
  identical segments and must still repaint.
- Background work that should become visible must trigger a repaint; it will
  not appear on its own.

## Platforms

macOS and all Linux are supported. **Windows is knowingly unsupported** — the
input path uses `termios`/`tty` and selects on `sys.stdin`, so the app cannot
start there at all. This is a deliberate, owner-accepted limitation
(2026-07-26). Do not add *new* POSIX-only assumptions, but do not attempt a
Windows port as a side quest either.

## Testing

**Tests must assert observable reality.** This is the most important rule here
and it was learned from a feature that shipped, passed a full suite for two
days, and had never written a single byte to disk (see INCIDENTS #2). Assert:

- bytes on disk, and their content — not that an index recorded an entry
- escape sequences and resulting screen state — not that a function returned a
  string (`vt.py` models a terminal including reflow-on-resize for exactly this)
- request counts — several tests assert that N rapid keypresses cost exactly M
  network calls

Tests must not touch the real network, the owner's real cache directory, his
token store, `~/Music`, or `~/.config/ticli`. A loopback HTTP server on
127.0.0.1 is acceptable and already used.

**The rails for that live in `tests/conftest.py`, and anything new that writes
at startup needs one there before it needs a test.** `DOWNLOAD_ROOT` and
`STATE_DIR` are both redirected suite-wide, autouse, because the alternative
is every future test remembering. The instance lock proved why: it was the
first thing `run()` touches, a handful of tests call `run()`, one of them
redirected the config but not the state, and the lock file duly appeared in
the owner's real `~/.config/ticli` on the first full-suite run. Per-test
redirection had been correct for years and was still one new startup write
away from being wrong.

**A flaky test is a bug until proven otherwise.** The one flaky test in this
project turned out to be a genuine race in cache eviction, not timing noise.

**Watch a new test fail against the unfixed code before you commit it.** Not a
style preference — a regression test that cannot fail is not a weak test, it is
not a test, and this project has now shipped that twice (INCIDENTS #2, #7). For
a race, hoping the scheduler cooperates is not enough: the first version of the
double-spawn test passed 25/25 against code with the bug fully present, because
CPython locks are unfair and the releasing thread barges. Pin the interleaving
so the failure is deterministic, and say in the test *why* the instrumentation
is faithful to what the scheduler does on its own.

## Honesty in the interface

Recurring theme, applied deliberately:

- **Never display a value or offer an option that isn't real.** The quality
  menu offered LOSSLESS and HIRES while TIDAL served AAC; gated tiers are now
  shown dimmed *with the reason* rather than hidden, because a hidden option
  looks like a missing feature while a labeled one explains itself.
- **Never fail silently.** Both audio backends once ran with stderr discarded;
  a whole library played as silence behind a normal-looking UI.
- **Distinguish loading, empty, and failed.** Three different states, three
  different renderings. "No albums" is information; a silent blank is a bug
  report waiting to happen.
- **Degrade honestly.** ffplay caps volume at 100% and the settings row says
  so, rather than accepting 250 and quietly ignoring it.

## Destructive operations

- **Only delete files ticli itself created.** Deletion enumerates an explicit
  list of owned filenames (`{track_id}{ext}` and `.part`); it never removes a
  directory and never globs-and-deletes. There is a test asserting a decoy
  file (`important.txt`) in the cache directory survives a full clear. The
  owner's framing: someone might have unrelated files in there.
- **Nothing is touched until a confirmation is answered.** Cancel is then a
  true no-op rather than a toggle-and-revert, which avoids a window where the
  setting is transiently off.
- Downloads (user-owned, `~/Music/Ticli`) are a separate tier from the cache
  (machine-owned, disposable) and are exempt from the budget and eviction.

## Working method

- **Delegate implementation to subagents; the main thread briefs and verifies.**
  The main thread's tool use should be limited to sharpening a brief (a
  targeted grep to locate a suspect), running the suite, reading the key hunk
  of a diff, and committing.
- **One subagent at a time by default.** The owner will sometimes ask for
  parallel work explicitly; isolate those in git worktrees. Don't infer
  standing permission from a past parallel batch.
- **Brief for evidence, not for compliance.** Agents told to "diagnose with
  measurements before fixing" disproved the main thread's leading hypothesis
  twice. Say explicitly that a refuted hypothesis is a valuable result.
- **Commit messages carry reasoning**, including measurements and rejected
  alternatives. This paid off directly: the history is reconstructable from
  the repo alone.
- **Update `ai/` in the same commit as the code.** A dated entry in
  `HISTORY.md` for every change; `DECISIONS.md` when something is decided,
  built or deferred; `INCIDENTS.md` when a failure teaches a rule. Correct
  anything your work made untrue rather than leaving it to rot, and write down
  what you *didn't* do — dropped scope and refuted hypotheses have been among
  the most valuable entries. See `ai/README.md` for what belongs where. This
  is a standing obligation on every agent, not a task that gets assigned.
