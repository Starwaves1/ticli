# Resume-malfunction trace — 2026-07-24 (two Opus 5 agents, verified empirically)

Symptom: "Resume is malfunctioning / really buggy behavior" while testing the
resume feature, mpv newly active as backend. Ranked, deduped findings.

STATUS 2026-08-07: **item 4 was only two-thirds fixed and is now closed** —
see the inline note on it, and INCIDENTS #7. Its third prescribed fix (the
re-entrancy guard on the stop→Popen window itself) never shipped in July; the
two that did only stopped the *monitor* from stepping into that window, and
the window went on double-spawning mpv for two more weeks until the owner
heard two songs at once. Treat the status line below as the cautionary case:
a multi-part fix is not done because its loudest symptom stopped.

STATUS 2026-07-24 (same day): items 1–5, 7, 8 (atomic writes), 9 FIXED —
20/20 tests pass, IPC ack + time-pos validated against a live mpv. Still
open: #6 (restore index stability on fetch failure — 2026-08-02: legacy
id-only files exclusively, see inline), ~~#8's multi-instance lockfile~~
(closed 2026-08-07, see inline),
#10 (_space_held swallow), #11 (get_url on UI thread — fold into the
responsiveness/caching roadmap item).

## Confirmed root causes

1. **Queue collapses to a single track** (likely the main thing Garrett saw).
   `_save_state`'s single-track fallback + the 10s autosave: `_restore_pending`
   clears in `finally` even when the restore failed or refused to attach the
   queue, so within 10s the autosave writes `[current_track.id]` — a 25-track
   radio queue was observed collapsed to its head track (15300277). Next
   launch: one song, next/prev do nothing, playback stops at song end.
   Fix: only clear the guard on successful attach (`_restore_ok` latch); drop
   or gate the single-track fallback; never autosave a smaller queue than what
   was restored.

2. **`is_paused` lies for a dead mpv.** `is_paused` returns the `_paused` flag
   with no liveness check; `is_playing` returns True while paused even if mpv
   exited. If mpv dies while "paused", space → resume() writes to a dead
   socket (exception swallowed), then the monitor auto-advances → pressing
   play skips to the next track (or snaps to 0:00 if last). The new
   restart-from-position branch in `_toggle_play` is unreachable in this case
   because `is_paused` is checked first.
   Fix: `_mpv_command` returns success (read mpv's reply); `is_paused` checks
   process liveness; reorder `_toggle_play` so dead-process falls through to
   `_play_track(track, seek)`.

3. **Pause during mpv startup window is a silent no-op.** mpv's IPC socket
   takes ~85–150ms (measured) to accept connections after Popen; pause inside
   that window is swallowed → UI shows paused/frozen timer while audio keeps
   playing; monitor is disabled (`_playing` False) so the app then sits
   "paused" forever after the track ends.
   Fix: poll for socket connect after spawn before first command; only set
   `_paused` on acknowledged command.

4. **Monitor false auto-advance during track change (~40ms+ window, 0.5s poll
   → ~8-10% of manual track changes).** `play_url` calls `stop()` first; between
   stop and Popen, is_playing/is_paused are both False while `_playing` is
   still True → monitor advances → one keypress skips two tracks; tighter
   interleaving double-spawns mpv → orphaned process, double audio.
   Fix: re-entrancy guard / "changing track" flag; require 2 consecutive dead
   polls before advancing.
   **2026-08-07 — fully closed.** The "changing track" flag (`_track_changing`)
   and the two-dead-polls rule shipped on 2026-07-24 and hold; they are why the
   skips-two-tracks half stayed fixed. The re-entrancy guard did not, so the
   double-spawn half never was: `play_url` went on releasing `_lock` between
   `stop()` and `Popen`. It is now one critical section (`_stop_locked` /
   `_play_url_locked`), with a regression test that asserts no spawned process
   is left alive but the current one. See INCIDENTS #7.

5. **Position is wall-clock, never read from mpv.** `_play_start_time` is
   stamped at Popen, ~0.3–0.7s before audio actually starts (measured) →
   position drifts ahead each start, autosaved, compounds across restarts.
   Also seek clamp `duration and seek >= duration - 2` bypassed when duration
   is 0/None → `--start` past EOF exits mpv rc=2 in 0.22s → resume instantly
   "skips". Fix: query mpv `time-pos` via IPC for position/save; clamp always.

6. **Restore fetch failures shift the queue.** `session.track()` raises (never
   returns None); dropped tracks shift indices → wrong song restored at 0:00;
   position never set if the current-track fetch fails.
   Fix: keep indices stable (placeholder/dict); set `_play_offset`
   independently of current fetch success; retry current.
   *2026-08-02: mooted for any file this build has saved — the record-format
   restore makes no fetches at all, so nothing can drop. Still true only for
   a pre-upgrade id-only file, where a non-rate-limit failure drops that
   track and shifts indices; a rate limit now ends the whole restore (no
   attach, toast) instead of shifting past it. Deliberately left: the legacy
   path is one save away from extinction.*

7. **Quit-save suppressed during restore discards a new position** (listen 30s
   after fast relaunch, quit before restore finishes → old position kept).
   Fix: merge position/current into existing file instead of skipping.

8. **State file writes are non-atomic and shared across instances.** Plain
   `write_text` from two threads (monitor + quit) and any concurrent second
   ticli instance (fixed path, not per-pid; two instances observed 14:01 +
   14:02) → torn/overwritten JSON → silent restore-nothing.
   Fix: temp file + `os.replace`; pid lockfile or read-only for late instance.
   **2026-08-07 — closed.** The atomic write shipped in July, which retired
   the *torn* half; what was left was the *overwritten* half, and this
   description overstated what remained — with `os.replace` in place two
   instances no longer tear the file, they take turns winning it. (A sharper
   version of the same hazard did survive and is also gone now: `.tmp` is one
   fixed path too, so two instances write the same temp file and the loser's
   `os.replace` can fail outright, swallowed at debug level by
   `_save_state_locked`.) Closed by making the second instance not exist:
   `_take_instance_lock()` in `run()`. Deliberately **not** the pid lockfile
   proposed above — an advisory `flock` is released by the kernel when its
   holder dies, so there is no staleness to detect and no recycled pid to get
   wrong, which is the whole failure mode of pid files. It refuses only on a
   positive answer; a filesystem that cannot lock (NFS) starts as before.

9. **No signal handling.** SIGHUP/terminal-close skips run()'s finally: no
   state save, no audio.stop() (explains stale /tmp/ticli-mpv-75221.sock; mpv
   dies with the process group so no orphan audio, but state is lost).
   Fix: SIGINT/SIGTERM/SIGHUP handlers + atexit fallback.

10. ~~**`_space_held` swallows a real space press**~~ FIXED (2026-07-25) —
    replaced by `_toggle_play_key()` and a `_last_toggle_key` timestamp with a
    0.15s repeat window. `_drain_stdin` deleted.

11. ~~**`track.get_url()` runs on the UI thread**~~ FIXED (2026-07-25) —
    `_play_track` now spawns a daemon thread and uses a `_play_gen` counter so
    a superseded request can't clobber a newer one.

## Verified non-issues

- mpv `--start=<float>` on TIDAL URLs seeks exactly (tested live, HTTP 206
  range OK, faststart moov). `cmd.insert(-1, ...)` ordering correct.
- mpv exits rc=0 at EOF (no --idle) — but leaves its socket file on disk, so
  socket existence ≠ liveness. A paused mpv stays alive indefinitely.
- mpv does not steal keystrokes from the shared TTY (tested).
- tidalapi Track has no `__eq__` → identity semantics in restore guard are
  correct today, fragile if tidalapi adds value equality.
- The ffplay `-ss`-from-URL change is unexercised while mpv is installed.
