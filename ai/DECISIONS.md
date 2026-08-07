# Ticli — Design Decisions & Roadmap

Project memory for AI-assisted development. (Tracked in git since 1927cab —
an earlier note here claimed ai/ was gitignored, which is no longer true.)
Last updated: 2026-08-02.

## Product goals

- **Feel:** perfectly smooth, instantly responsive, roughly zero power draw.
- **Compatibility first:** must work everywhere — macOS, all Linux, Windows Terminal.
  Avoid platform-exclusive techniques in core features; platform integrations
  (e.g. macOS media keys) are additive layers, never requirements.

## Decisions (locked)

1. **Reopen behavior** — restore last track at its last position, **paused**.
   Never autoplay on launch.
2. **Artwork** — very basic pixel art (half-block Unicode, works in any terminal)
   is the chosen approach. Default **on**, toggle in settings, rendered results
   cached so it costs nothing on re-display.
3. **Caching** — default cache budget **under 2 GB**, with a setting to raise or
   lower it. Granularity setting: full song caching vs UI/metadata-only caching.
   (Note: metadata + artwork cache is only a few MB; the 2 GB budget is really
   about cached audio.)
4. **Roadmap order** (approved by Garrett):
   1. Resume-last-song — **BUILT 2026-07-24, awaiting Garrett's testing.**
      Restores last track paused at saved position; space resumes from there;
      state autosaves every 10s (crash-safe); restore fetches current track
      first so it appears before the rest of the queue loads; restore never
      clobbers playback the user starts in the meantime. Tests in
      `src/ticli/tests/test_resume.py`. (2026-08-02: the state file now also
      carries flattened track records, and restoring from them costs **zero**
      requests — the fetch-per-id behavior described here survives only as
      the legacy path for pre-upgrade files, now paced and stop-on-429. See
      HISTORY.) Also fixed: ffplay fresh-play-with-seek
      used the just-started (empty) cache file — now seeks the URL directly.
   2. Add-to-playlist — **BUILT 2026-07-24, awaiting Garrett's testing.**
      `y` key in player/queue/browse opens a picker of editable playlists
      (isinstance UserPlaylist filter, cached 60s), Enter adds (server-side
      duplicate skip → "Already in" toast), toast infra added (reusable).
      Deferred to follow-ups: "+ new playlist" row, last-used-playlist
      pin-to-top. Tests in `src/ticli/tests/test_add_to_playlist.py`.
      Follow-up same day: `x` removes the cursor track when browsing one of
      the user's own playlists (remove_by_index, busy-guarded); footer label
      changed "[y] playlist" → "[y] add to playlist". Committed as fa62887.
      Media-keys spike: Opus 5 agent running in a git worktree, hard
      constraints = zero new deps, zero non-mac impact; mpv-native route
      preferred. Garrett's git identity unconfigured (commit fa62887 has
      hostname email) — told him how to fix.
   3. Config module + settings page — **BUILT 2026-07-24, committed 9e2b2aa,
      awaiting Garrett's testing.** `c` opens Settings (quality, page size,
      progress-bar width); `~/.config/ticli/config.json` (atomic writes,
      unknown keys preserved); `--quality` now overrides for that run only
      without persisting. `SETTINGS_SPEC` table in `utils/config.py` — future
      settings (artwork toggle, cache size) are one new row each. 36 tests in
      `test_config.py` (71 total).
   4. Search overhaul — **PARTIALLY DONE 2026-07-24 (commit 5dd12c3):** real
      page sizes shipped. Search was hardcoded `limit=8` split 5/3/2; now
      issues one request at `self._page_size`, split 50/30/20 with unused
      rows from thin categories handed back to tracks. Verified in tidalapi
      `session.py:771-789`: `search(query, models, limit=50, offset=0)`,
      max 300, limit applies per type. `_open_artist` top-tracks now
      `max(20, page_size)`; `get_track_radio(limit=25)` left alone (queue
      fill, not a page). STILL OPEN: type filters, fetch-more-on-scroll.
      Same commit fixed a real quality bug — tiers were off by one
      (setting "LOW" → low_320k, "HIGH" → high_lossless, so the top two
      were identical and 96k unreachable); names now map 1:1 to tidalapi
      (`media.py:57-62`), settings page shows what each tier streams, and
      a config v1→v2 migration renames saved values so nobody is silently
      downgraded. Wraparound (→ past HIRES → LOW) already worked via
      `cycle_value`'s modulo; now test-locked for all choice settings.
      167 tests. Needs Garrett: confirm the four tiers audibly differ and
      that his existing config migrates without a perceived change.
   5. Caching / responsiveness (playlist cache, prefetch; enables
      search-within-playlists via local index)
   6. Artwork (pixel art)
   7. Media keys (macOS) — **BUILT 2026-07-24 in a worktree, VERDICT GO.**
      Zero new deps: mpv's built-in MPRemoteCommandCenter works headless;
      ticli rebinds the 7 media keys over IPC (`keybind` priority 15) to a
      `user-data/ticli/media-key` property, polled by the existing 0.5s
      monitor tick; `--force-media-title` shows "Track — Artist" in Control
      Center. IS_MACOS-gated, other platforms inert. 30 tests vs fake mpv
      IPC server. Branch pushed as `media-keys-macos` (commit a5942f3);
      merges cleanly with settings commit (verified via merge-tree).
      MERGED to main locally by Garrett (be4f6e8, his first worktree merge;
      GitHub PR abandoned — API 500'd persistently on PR creation for this
      fork). 101 tests pass on merged main. Garrett manually VERIFIED same day:
      "Media keys are great!" Linux MPRIS / Windows SMTC deliberately not
      attempted (would need optional deps).
      Follow-up round — **BUILT + committed bf95d96 2026-07-24, awaiting
      Garrett's testing.** Logout `o` + logged-in-as moved to settings
      (o inside settings; player screen cleaned); smart prev (>30s =
      restart song — exclusive threshold, gapless mpv seek w/ respawn
      fallback — both ← and PREV media key); volume setting 0-100 step 5
      (live via mpv IPC, spawn-time for both backends; ffplay picks up
      next track). 135 tests. Agent flagged for manual check: mpv
      `seek 0 absolute` on TIDAL streams (NACK → audible-gap respawn
      fallback, still correct) and ffplay `-volume` acceptance.

## Downloads + play-count eviction (both **built** 2026-07-26)

**Eviction and admission shipped**, on a **cache tracker** —
`CACHE_DIR/audio.json`, Garrett's framing: *"caching needs a cache tracker and
the actual cached files… the cached files are merely a downstream result of
that."* Per track it holds the extension, the tier TIDAL granted, the size,
the play count and our own last-played stamp. It is the authority on **intent
and metadata**; the disk stays the authority on **existence**, which is how
his other standing requirement — *songs deleted from the folder by hand must
be handled durably* — survives. `MetadataCache.reconcile()` is the one place
the two meet: entries with no file are dropped, files with no entry are
adopted at zero plays and no known tier, sizes are corrected. Daemon thread at
startup; never a directory walk on a paint.

**The rule as built.** `audio_value(track_id, playing)` → `(plays, last
played)`, one function read by both eviction and admission so they cannot
drift. Eviction takes the lowest value first: fewest plays, then oldest — his
original design, and not LRU, because a four-hour binge must not evict
staples. A half-written `.part` sorts below every song. A play is counted
once, on the monitor's existing tick, after 30 seconds (or half of a shorter
track): **a skip is not a play**. The stamp is ours, never `atime`.

**Admission refuses only under pressure**, as decided below. The freeze
problem — a new track has zero plays, so a full cache would refuse everything
for ever — is answered by `playing=True`: *the song being listened to counts
the play it is earning right now*, so it displaces the oldest **other**
one-play track and nothing else. Same rule, no special case, no scratch tier
needed. And deliberately expressible at any moment, so moving the decision to
track end (where "was it really listened to?" is answerable) is a change of
caller and not of rule — `_admit()` is that single caller.

**Downloads shipped.** `[d]` from the player, browse, artist and queue screens
opens a single-column quality picker, cursor pre-placed on the settings tier;
the hovered tier says `download now` and every tier shows its estimate. Files
land in `~/Music/Ticli/<Album artist>/<Album>/<NN> Title.m4a`, exempt from the
budget and from eviction by construction (both work from `audio_dir()`), and
are tagged in place by a stdlib MP4/FLAC tagger (`utils/tags.py`) — no mutagen,
no ffmpeg. **Play-count eviction below is still deferred at Garrett's request.**

Size-estimate calibration, redone for FLAC with zero network (the numbers in
the research doc were AAC-only): a real 24/88.2 PKCE track out of this
machine's own cache, transcoded locally to 16/44.1, came to 765 kbps — 54% of
PCM — with twelve-second windows spanning 502-852 kbps. Nominal is 850 kbps
for LOSSLESS and 2500 kbps for HIRES, and the screen says out loud that FLAC
is variable where it says AAC is not.


Research: `ai/reference/download-research-2026-07-25.md` (live-probed).

Garrett's eviction design: each track gets a point per play; evict the
oldest among the tracks with the fewest plays (all 1-play tracks oldest
first, then 2-play, etc). Rationale: a 4-hour binge on a new playlist
must not evict long-term staples, which pure LRU does.

Refinements from Garrett 2026-07-25:
- **Do not use filesystem `atime`** (report flagged `relatime` makes it
  ~daily-granular). Stamp `time.time()` ourselves at play time and store
  it — we control the write, so there is nothing unreliable about it.
- **The whole play-count/eviction path is active only when song caching
  is enabled in settings.** Metadata-only mode must not run it.
- Settings shows a **tiny count of downloaded songs next to the song-cache
  toggle**.
- Downloads are a separate user-owned tier: `~/Music/Ticli`, path shown in
  settings, **exempt from eviction and the cache budget** (a deliberately
  downloaded 1-play track must not lose to an auto-cached 2-play one).
- Manual deletion from the songs folder must be handled durably by the
  caching, downloading and playback paths.

Download UI (`d` in the more menu): download screen, Enter starts;
"calculating size" indicator; quality picker as a **single column**, cursor
pre-placed on the tier selected in settings; hovering a tier shows
"download now" to its right and the size estimate to the right of that.
Size estimates cost **zero requests** (duration × nominal bitrate, −0.3% to
−2.1% error on AAC; duration already in the cache index) so all four tiers
can be shown at once — label with `~`.

## Cache admission (Garrett, 2026-07-26 — **built** 2026-07-26)

Eviction was deferred earlier; admission came up from the other end:

> "If a song playing now isn't worth caching in a circumstance where there
> are a bunch of very high value songs already cached, it should just stream
> in chunks and not cache."

**Decided:** refuse to cache **only when the cache is already full** and the
playing song is lower value than what is in there. With room to spare,
behaviour is unchanged — cache everything, as today. Rationale: at the moment
a song starts you know almost nothing about it (zero plays, like every staple
on its first day), so an unconditional value test would freeze the cache into
whatever it held the day the rule was turned on. Only under pressure do both
sides have a history to compare.

**Settled and built 2026-07-26:** the value function is **plays-then-oldest**,
his original eviction design, shared by both halves as one function
(`audio_value`). The candidates that lost: "cache on second play" is the
freeze in another costume, and "played most of the way through" is a better
signal but is not available at the moment the download starts — it becomes
available if the decision moves to track end, which the code is shaped for
and which changes the *caller*, not the rule.

The downloads tier stays outside admission entirely, the way it is already
outside eviction and the budget: `~/Music/Ticli` is not reachable from
`owned_audio_files()`, and nothing in the admission path looks at it.

**Both halves matter to him** — "Both eviction and admission are important."

## Single-fetch playback + the scratch tier (Garrett, 2026-07-26 — proposed)

Today a played track with `cache_songs` on is fetched **twice concurrently**:
the backend streams it while `_start_download` fetches the same bytes. His
proposal: play the first ~30s from a partial download, and at ~20s — if the
user is still listening and the song is worth caching — fetch the rest and
resume from the local copy, so one download serves both streaming and caching.
Fallback he named himself: keep streaming in chunks if switching proves hitchy
or expensive.

Plus a **scratch tier**: when the cache is full, do the same thing anyway and
delete the file when the song ends or ticli closes. It does not count against
the budget.

**Reframing that must survive:** the hitching that motivated this is already
fixed (`c71de29` — mpv's `--cache` had defaulted off because it is handed a
local playlist file, leaving 1.02s of readahead against 4s segments). So this
is about **bandwidth, not smoothness**, and a mid-track file switch now has to
justify a gap it might *introduce* rather than curing one. `_start_download`
also already fetches the whole track in seconds, so the "first 30s, then the
rest" staging may not be load-bearing.

Feasibility measured under `ai/reference/single-fetch-feasibility-2026-07-26.md`.
Depends on the `is_owned_audio` `.part` fix (data-path audit finding 1) landing
first, or it fills the cache with unrecognisable leaked files twice as fast.
The "worth caching" hook must default to today's behaviour — the value function
is still open (see cache admission above).

## The cache tracker is the source of truth (Garrett, 2026-07-26 — locked)

> "Caching needs a cache tracker and the actual cached files. The cache tracker
> will be updated and the cached files are merely a downstream result of that."

Admission, eviction, play counts, timestamps and the stored quality tier are
recorded in the **tracker**; files on disk are reconciled to it. Eviction is
"the tracker says this is gone, remove the file", not "walk the directory and
decide as you go". The settings metrics read the tracker, which is also the
answer to the audit's O(N²)-on-the-UI-thread finding: reading a maintained
index is cheap, walking the disk every paint is not.

**Reconciled with his earlier, emphatic requirement** that manually deleted
files be handled durably: the tracker is authoritative for **intent and
metadata**, the disk is authoritative for **existence**. A tracker entry whose
file is missing is a fact to absorb — drop the entry, correct the totals — not
an error and not something to ignore. Verify at the moment of use; never walk
the directory on the UI thread to find out.

## A downloaded copy always wins (Garrett, 2026-07-27 — locked)

> "Check that a downloaded song (not cached song) is never unconsentually
> upgraded in quality when it is streamed at a higher quality. It should just
> stream from download and not stream data."

**A download is played whatever the quality setting says.** No tier
comparison on the download tier at all: raising the setting to HI-RES is not
a request to re-fetch songs already in `~/Music/Ticli`, and streaming them
spends the user's data on audio he deliberately keeps on his own disk.
Quality changes for a download **only** when he asks — `[R]`, re-fetch
everything at the current quality. That is the consent, and nothing else may
borrow it.

**The cache is the opposite tier and keeps the opposite rule.** A cached copy
below the current setting is still passed over and re-fetched. The cache is
machine-owned, disposable and evictable; the download is the user's file. The
two tiers differ in ownership, so they differ in who gets to replace them.

Consequence, accepted deliberately: the player's quality badge shows what is
**really playing** rather than what is selected whenever a download is the
source (`LOSSLESS · downloaded`; a bare `downloaded` when the tier was never
recorded). Under "never display a value that isn't real", a badge reading
HI-RES over a LOSSLESS download is the same class of lie as a quality menu
offering tiers TIDAL never served. Status line only — no toast, because it is
true for every track of a downloaded album — and no setting.

## Quality tiers are named TIDAL's way (Garrett, 2026-08-02 — locked, built)

From a screenshot of the official app's quality screen: **ticli's tier names
match TIDAL's player** — Low / High / Max — **with the 320k AAC rung surfaced
as MEDIUM** (the app files it under Low's bitrate dropdown without naming it;
"Set 320k to medium" is his wording). Built 2026-08-02: `LOW / MEDIUM / HIGH /
MAX` across settings, CLI, badges and the download picker; config v5
migration renames saved values (the v1 precedent, reused); `LOSSLESS`/`HIRES`
live on as CLI aliases; `--quality HIGH` — the one spelling that changed
meaning — announces its new meaning on stderr each use.

Sub-decision, initially made without him as tier-name badges, **overturned by
Garrett the same day**: *"I'd like formats even if sometimes it falls back to
a slightly different quality."* The badge is now the stream format —
`96k AAC / 320k AAC / 16/44.1 FLAC / 24/192 FLAC` — with the overclaim risk
on MAX (a nominal 24/192 label over a master that is really 24/48) accepted
by him explicitly. `QUALITY_LABELS` in player.py is the single seam, and the
settings ladder shows the format beside each tier name again (70 columns for
all four, still inside the page's 80-column budget).

## The README documents mpv only (Garrett, 2026-08-02)

*"MPV pog yes. Dropped ff"* — both READMEs now name mpv as the player and
requirement; ffmpeg/ffplay no longer appear in install, requirements or the
diagram. **The ffplay fallback is still in the code** (`AUDIO_PLAYERS`,
volume clamping, seek-by-respawn, its tests): removing it is real surgery and
was not asked for. Open question: delete the ffplay path, or keep it as the
undocumented zero-install fallback?

## Settings page at small sizes (Garrett, 2026-07-26 — locked)

> "Accept that settings will not work well in very small windows. That's fine.
> The current behavior is good."

The bar stays where `9c96d63` left it: fits at **80x24** with `[x]`, `[o]`,
`[u]` on screen, and never overflows at any size. Below that, degraded is
explicitly acceptable. Do not add levers chasing it.

## Backlog (requested, not yet scheduled)

- **`r` (start radio) must not restart the current song** (Garrett,
  2026-07-26). Today `_start_track_radio` rebuilds the queue and replays the
  current track from 0:00. Two acceptable shapes, in order of preference:
  (1) rework the radio path so it swaps the *upcoming* queue without touching
  the playing track at all — no restart, no gap; (2) if a restart is
  unavoidable, minimize the hitch by reusing the pause/resume machinery
  (position is already tracked; mpv can seek in place, and the cached-file
  path avoids a refetch). Preference is strongly for (1) — his words: "if
  it's possible to cleanly rework the machinery which makes a radio happen to
  not require a song restart, that'd be great."

- **Second worktree, parallel with the main roadmap** (Garrett, 2026-07-24):
  spawn a separate Opus 5 in its own worktree for the small stuff, so two
  Opus 5s run in parallel — (1) ↑-at-top-of-list focuses player, ←/→ seek
  ±10s; (2) "+ new playlist" row in the picker + pin last-used playlist to
  top; (3) the three open bugs: restore index shift on fetch failure,
  ~~multi-instance state-file clobbering~~ (**built 2026-08-07**, see the
  single-instance section below), `_space_held` swallowing a keypress
  within 250ms of another.
- **Opus 5 prompting** (learned 2026-07-24, saved to user memory): delete
  "verify your work" and "delegate more" instructions from agent prompts
  (Opus 5 does both on its own and over-does them when told); add conciseness,
  deliverable-length, scope-discipline, and a subagent cap. Sweep effort
  downward — low/medium are unusually strong.

- **In-song seek via up-arrow focus** (Garrett, 2026-07-24): pressing ↑ when
  already at the top of a list shifts focus to the player/progress bar; ←/→
  then seek back/forward in 10s increments. Needs an `AudioPlayer.seek()`:
  trivial with mpv (IPC `seek N relative`); ffplay needs kill + restart from
  cache with `-ss` (machinery for that mostly exists in `_play_from_cache`).

## Only one ticli at a time (2026-08-07 — **built**, owner confirmation pending)

Built in response to the owner's report of "a primary song playing and another
song playing at that same time", which has two causes; this closes the second.
It also closes the multi-instance state clobbering he listed as an open bug on
2026-07-24 (see Backlog, and BUGS-2026-07-24 item 8).

**The decision that is his, not mine:** a second instance is **refused**, with
a message naming the pid to go and quit. The alternative on the table was
starting it read-only — no state saves — which would have fixed the clobbering
and left the two-songs symptom entirely untouched, so it was not really an
alternative for the reported bug. Refusing is nevertheless the stronger claim:
it removes something that used to work, even if what it used to do was
misbehave.

Worth his answer on one thing specifically: **is a second window ever wanted
for browsing while the first plays?** If so that is a distinct feature — an
instance that never takes the audio backend and never writes state — rather
than a loosening of this guard, and it should be built as such. Until he says
otherwise, refusal stands.

Not a decision, a property: the guard is best-effort by design. A filesystem
that cannot take an advisory lock (NFS home directory) starts as it always
did, because being locked out of your own music player by a guard is worse
than the bug it guards against.

## Open questions

- Whether a second ticli should ever be allowed for browsing-only — see the
  single-instance section above; refusal is in place pending Garrett's call.
- ~~mpv adoption~~ — **DECIDED: Garrett installed mpv 2026-07-24.** ticli
  auto-prefers it; ffplay remains the zero-extra-install fallback. Real
  pause/resume via IPC socket now active on his machine.
- Keybinding for add-to-playlist (research suggested `y`, verified free in
  player/queue/browse modes).
- Settings page contents/layout details.
- Search-within-playlists: no server-side TIDAL API for it; requires the local
  playlist index from the caching work (item 5), not the search work (item 4).

## Working agreements

- Garrett is learning hands-on: give him commands to run himself where it helps.
- "Brainstorm/plan" = discuss with him before launching agents or writing docs.
- Deep per-feature design research (7 features, done against the real codebase)
  is saved at `ai/reference/feature-design-research-2026-07-24.json` — use as
  reference material when building each item, but decisions above win.

## Codebase facts worth remembering

- `src/ticli/player.py` (~1450 LOC) is the whole app; modes PLAYER/SEARCH/
  BROWSE/QUEUE/PLAYLISTS, Rich Live at 4fps, daemon threads for network,
  lock-free reads via whole-object assignment (GIL-reliant; never mutate
  shared lists). Since 2026-07-27 a multi-writer load-modify-save cycle over
  one on-disk JSON file gets a leaf lock — cache tracker, then player state
  (2026-08-02); see WORKING-RULES.
- State already persists to `~/.config/ticli/player_state.json` (queue ids,
  index, position, search history) — resume feature mostly needs restore-side
  work.
- Search currently fetches only 8 results total (5 tracks / 3 albums /
  2 artists) — root cause of "search feels bad".
- `PAGE_SIZE = 15` and progress-bar width are hardcoded — no config system yet.
- Dev setup: pipx editable install (`pipx install -e src/`), keyring injected;
  edits live on next `ticli` launch. `src/.venv` from CLAUDE.md doesn't exist.
