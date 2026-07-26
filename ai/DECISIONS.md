# Ticli — Design Decisions & Roadmap

Project memory for AI-assisted development. Not committed (ai/ is gitignored).
Last updated: 2026-07-24 (initial brainstorm with Garrett).

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
      `src/ticli/tests/test_resume.py`. Also fixed: ffplay fresh-play-with-seek
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

## Downloads + play-count eviction (downloads **built** 2026-07-26)

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

## Cache admission (Garrett, 2026-07-26 — partially decided)

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

**Still open, Garrett is thinking about it:** the value function itself, shared
by eviction and admission. Candidates discussed — cache on second play; cache
only if the track was played most of the way through; plays-then-oldest (his
original eviction design). Also open: whether the downloads tier
(`~/Music/Ticli`) sits outside admission entirely, the way it already sits
outside eviction and the budget.

**Both halves matter to him** — "Both eviction and admission are important."
Do not build either until the value function is settled.

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
  multi-instance state-file clobbering, `_space_held` swallowing a keypress
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

## Open questions

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
  no locks (GIL-reliant; assign whole objects, never mutate shared lists).
- State already persists to `~/.config/ticli/player_state.json` (queue ids,
  index, position, search history) — resume feature mostly needs restore-side
  work.
- Search currently fetches only 8 results total (5 tracks / 3 albums /
  2 artists) — root cause of "search feels bad".
- `PAGE_SIZE = 15` and progress-bar width are hardcoded — no config system yet.
- Dev setup: pipx editable install (`pipx install -e src/`), keyring injected;
  edits live on next `ticli` launch. `src/.venv` from CLAUDE.md doesn't exist.
