"""The text behind `ticli agent docs`: the complete contract for programs.

Rendered, not stored: the verb list comes from the `ticli.commands` registry, so
a new command shows up here with its cost and arguments and cannot go
undocumented (a test requires every registry command to appear). Only the
prose around it is written by hand. The one verb that prints markdown rather
than JSON, because its reader is a language model.
"""

# Agent verbs that predate the registry and keep their own names and keys.
LEGACY_VERBS = {"library.playlists": "playlist list", "playlist.tracks": "playlist show"}

HEAD = """\
# ticli agent: the contract for programs

ticli is a terminal music player for TIDAL; the TUI is the human's surface and
`ticli agent` is yours: headless verbs that print JSON, paced in code. This page
is everything you can do. To work on ticli's *source*, read `CLAUDE.md` instead.

## Reply shape

Every verb prints **one JSON object** on stdout and exits (`docs` is markdown).
```json
{"ok": true, "result": {...},
 "state": {"track": {"id": 1, "title": "T", "artist": "A", "pos": 12, "dur": 200,
                     "liked": false, "quality": "HIGH"},
           "playing": true, "queue": {"len": 3, "index": 0},
           "switches": {"ai": true, "dangerous": false}, "connectivity": "online",
           "pending": 2},
 "next": ["pause", "next", "queue list"],
 "cost": {"requests": 1, "wait_s": 4.0, "eta_s": 10.0}}
```
- `state` is a snapshot after the call (`track` is null when nothing is loaded;
  `pending` appears only while agent commands wait). `next` is up to 5 verbs
  that apply right now. `cost`: TIDAL `requests` used, `wait_s` you waited in
  the queue, `eta_s` until the last queued command finishes. Track durations
  are `duration_seconds` everywhere (`dur` in `state`). `state.track.liked` is
  whether it is in the user's favourites; `quality` is the tier TIDAL granted for
  it (`LOW`, `MEDIUM`, `HIGH`, `MAX`; null until known).
  `state.jobs` appears while a download or re-fetch runs (`state`, `done`, `total`
  or `tracks`, `failed`); `status` shows the last one even when finished.
- Every action you take shows in your human's TUI as a short `agent: ...` line
  ("agent: queued 3 tracks"); they watch what you do.
- Failure: `{"ok": false, "code", "reason", "fix"}` and exit 1. Codes:
  `ai_control_off`, `dangerous_off`, `key_required`, `wrong_key` (permissions),
  `rate_limited`, `not_logged_in`, `auth_failed`, `not_found` (stale or wrong
  id), `stale` (the queue moved; the reply carries the current `queue`),
  `bad_args`, `empty`, `no_track`, `not_yours` (a playlist you don't own), `human_only`, `api_error`,
  `player_unavailable`, `offline`, `signed_out`. Act on `fix`. The original verbs (`search`, `resolve`,
  `playlist ...`) also keep `error`, `message`, `hint`, and their own result keys.
- Arguments are positional in the order shown below, `key=value`, or one JSON
  object. Ids are best (from `search`, `resolve`, `playlist list`); a playlist,
  album or artist may also be a name (your own playlists first, 0 requests;
  else 1 search; with AI control off only your own playlists) and a song a
  TIDAL URL or `"artist - title"` (added only on a confident match). Ambiguous
  ones fail with `candidates` carrying ids: pick one, pass its id.

## The queue: pacing, ETAs, coalescing

Every verb that reaches TIDAL waits in one queue in the player, shared by all
agents, **requests 2 s apart**; you never pace by hand and cannot bypass it.
- **Reads** (`search`, `resolve`, `playlist list/show`, `album tracks`,
  `artist section`) block until answered and return the data; `cost.wait_s` is
  what the queue made them wait.
- **Actions** (play, like, playlist add, download...) answer at once with
  `result.queued` (position) and `result.job`; `cost.eta_s` is when the last
  one should finish. `ticli agent status` lists `pending` and recent `done`
  (a merged job's row says `merged`, and a partial add `added`, `failed_from`,
  `not_added`). `playlist create` waits and returns the new playlist.
- **Local** commands (pause, queue edits, settings) cost 0 requests and run at
  once, unless agent commands are already queued: then an action takes its place
  in line (`next` then `pause` ends paused) and a local read still answers at once.
- **Downloads** take one track at a time, each through the same 2 s spacing.
- **Coalescing**: waiting adds to one playlist merge into one add (up to 100 ids
  per request, plus TIDAL's re-read: 2 requests for 1-100 ids); waiting likes
  merge too. The reply says so in `result.merged`:
  batch them, never add in a loop.
- **`ticli agent do`** runs a JSON array in order:
  `ticli agent do '["playlist add ID 1 2", "like 3", "queue list"]'` (or the
  array on stdin; items may be `{"cmd": "playlist.add", "args": {...}}`). Local
  items before the first TIDAL item run immediately; from that item on, every
  item queues in order, submitted together so adds and likes coalesce. A refused
  item, or an immediate one that fails, skips the items after it (`"code":
  "skipped"`); items queued before it still run. A queued item that fails later
  skips nothing. The reply's `result` is one record per item, in order: run items
  carry `result`, queued reads and `playlist create` wait and carry `result`,
  queued actions carry `queued`, `job`, `eta_s` (and `merged`; their outcome is in
  `status`). `ok` is true only if every record is; `cost.requests` counts what the
  waited items used plus the estimate for queued actions.
- Queue entries: pass the `track_id` you saw with the index
  (`queue remove 2 TRACK_ID`, `queue move 5 1 TRACK_ID` to put entry 5 at 1); if the
  queue moved you get `stale`, not another track. `queue clear` keeps only the
  current track; neither touches playback.
- Adding to the play queue: `queue add ID... [--next]` (or `album=ID`, `playlist=ID`,
  `mix=ID` for all of a list's tracks; a song may be `"artist - title"` or a URL).
  Default is the end; `--next` plays it after the current track. It never replaces the
  queue and never starts playback: with nothing playing it says so in `result.note`
  (and with nothing loaded the first track becomes current, paused). Known tracks cost
  0 requests; each unknown id is 1, waited for in the queue. One unknown id queues
  none (`not_found`). Duplicates are allowed. The reply: `added`, `position`, `index`
  (of the first added entry), `queue_length`, the first 10 `tracks`.

## Offline

`state.connectivity` is `online`, `offline` (TIDAL unreachable) or `signed_out`
(TIDAL rejected the stored login; only your human can sign in again, with [o]
in the TUI). The player never probes: a verb that needs TIDAL tries to reconnect
once, then answers. Offline:
- Reads answer from what was opened before, with `"offline": true`, `cached_at`
  and `age` ("cached 3 days ago"); never opened is `offline`. `search` and
  `resolve` answer from your playlists, favourites and downloads
  (`"source": "local"`).
- Writes (like, playlist edits, download) are refused with `offline`, never queued.
- Downloads and cached songs play: `play downloads INDEX`, `play track ID`; `next`
  and auto-advance skip entries with no local copy.

## The trip

TIDAL rate-limits by IP; a block stops the owner's music too. On a 429 or
bot-detection response everything agent-side trips and fails fast with
`rate_limited`. Then stop and report to the human, from your own tally of
what already completed; spend nothing reconciling. `ticli agent unblock` is
Human-only: it refuses without a terminal. Never retry; retries extend the block.

## Permissions: your human decides, you ask

Three switches in ticli's TUI settings. **Only the human can change them**:
no command or verb can. **Ask your human; never edit config.json, write ticli's
files or impersonate the TUI.** Changes show in the TUI.
- **Allow AI control** (on by default). Off: actions are refused with
  `ai_control_off`; reads (`status`, `queue list`, `download list`,
  `settings get`, `cache status`, `playlist list`, `search` over your own playlists) answer from
  disk, 0 requests, `"source": "disk"`, and never start the player.
- **Allow dangerous commands** (off by default): deleting, renaming or
  re-describing playlists, deleting downloads, removing playlist tracks,
  re-fetching the library, clearing the cache, lowering the cache budget, logout,
  changing the login flow. Refused with `dangerous_off`.
- **AI control key** (unset by default). Set: every verb except `status` needs
  it via `TICLI_AI_KEY` or `ticli agent --key KEY <verb>`; wrong keys cost 1 s.
On any refusal quote its `fix` to your human and stop.

`ticli <verb>` (no `agent`) without a terminal on stdin is treated as you too:
same switches and key, readable text output, the same names and songs (plus
`current`); ambiguous ones return `candidates` with ids as JSON.

## Verbs
"""

TAIL = """
## Workflows

**The user names songs; you build a playlist.** `resolve` each song, `playlist
create`, then one `playlist add` with every id (or one `do`). Add only what a
`confident` resolve returned or the human chose from `candidates`; report the
rest by name. N songs is about N+2 requests, 2 s each: say so for long lists.
`resolve` ranks strictly: the artist is a gate, unrequested remixes/live
versions are demoted (`unrequested_qualifier`), `feat.` credits are ignored;
`confident` means right artist, exact title, no unrequested qualifier.

**"Queue this song" / "play X next".** `queue add "artist - title"` (add `--next`
for "next"), or `resolve` first and `queue add ID`. Never `play track`: that replaces
the queue.

**"Add this to my X playlist."** `playlist list`, match X case-insensitively;
more than one match or no match: ask. Ask first; never auto-create a playlist the user called existing.

**"More results."** `search QUERY --offset 10` is the next page of 10 (1 request each).

**"Upgrade my music to FLAC."** `refetch plan` first (0 requests): it says how many
songs, the requests (2 per song) and the time (2 s per song at least). Tell your
human those numbers; run `refetch` (dangerous) only on their yes. A tier the login
isn't served comes back in `note`.

**"Delete / rename that playlist."** Only by id from `playlist list` (`playlist
delete` refuses names); say the name back to your human and get their yes first.

**"What is this track?"** `track info ID`: album, duration, explicit, the tier TIDAL
offers, liked, and whether it is downloaded or cached; 0 requests when known locally.

**Setup questions.** `status` is free: `flow` is `pkce` (FLAC) or `device`
(AAC only); `flac_capable` false means the human presses `u` in TUI settings.

## Ground rules

- This is the only sanctioned path. Importing ticli's internals or calling
  TIDAL directly bypasses the pacing and got the owner's IP blocked.
- Playlists you create are real TIDAL playlists, visible everywhere at once.
- One login is shared with the TUI. Never touch the credential store;
  `not_logged_in` means the human runs `ticli`.
"""

STATUS = """
### `ticli agent status` — 0 requests
Setup and what is happening, free, never starts the player. Always answers,
even with a key set or AI control off.
```json
{"ok": true, "ai_control": {"allow_ai_control": true, "allow_dangerous_commands": false,
 "key_required": false}, "session_stored": true, "flow": "pkce", "flac_capable": true,
 "player_running": true, "throttle": {"min_interval_seconds": 2.0, "tripped": null},
 "state": {...}, "pending": [...], "done": [...]}
```
`--verify` spends 1 request to confirm the login works (`"verified"`); it proves the login, not the audio.
"""

LEGACY = """
### `ticli agent resolve --artist A --title T [--limit N]` — 1 request
THE verb for "the user named a song". Answers `{"confident": bool, "best": {...},
"candidates": [...]}`; candidates are `{id, title, artists, album,
duration_seconds, explicit, artist_match, title_exact, unrequested_qualifier, score}`.

### `ticli agent search QUERY [--type track|album|artist|playlist] [--limit N] [--offset N]` — 1 request
One request however many `--type`; `--offset` pages (each page 1 request). Output `{"query", "tracks": [...], ...}`: tracks as
above minus ranking; albums `{id, title, artists, num_tracks, year}`; artists
`{id, name}`; playlists `{id, name, num_tracks, description}`.

### `ticli agent playlist list|show|create|add`
`playlist list` (1 request) `{"playlists": [...]}`; `playlist show ID` (2)
`{"playlist", "tracks"}`; `playlist create NAME [TRACK_ID...] [--description D]` (1, plus
2 per 100 tracks) waits and answers `{"playlist": {"id"}, "added"}`, capture the id; `playlist add ID TRACK_ID...` is queued
and coalesced (2 requests per 100 ids); the reply has `requested`, `queued`, no
`added` (not known yet): check with `playlist show`.

### `ticli agent restart` — 0 requests, or 2 to resume an uncached track
Replaces the background player with one on ticli's current code; the queue, track,
position and playing/paused carry over (about a second of silence). Answers
`{"restarted", "running", "loaded", "playing"}`; `running: false` means no player
was up. Refused with `busy` while a download, re-fetch or your queued commands run.
Clients already do this on their own when the player is older and idle or only
playing; an `unknown_command` whose reason starts "The background player is
running older code" means it couldn't: run `restart`, then the command again.

### `ticli agent docs`, `ticli agent do`, `ticli agent unblock`
`docs` prints this page (0 requests, markdown). `do` is described above.
`unblock` is Human-only.
"""


_ONE_PLAYLIST = "1 request (2 if the playlist isn't loaded yet), waits its turn and returns the result"
COSTS = {"queue.add": "0 requests for tracks already known, else 1 per unknown id; "
                      "waits its turn and returns the result",
         "track.info": "0 requests when the track is known locally, else 1; returns data",
         "refetch.plan": "0 requests: what `refetch` would do, songs, requests and time",
         "refetch": "2 requests per song, one song per 2 s, for the whole library; "
                    "run `refetch plan` first and ask your human",
         "playlist.delete": _ONE_PLAYLIST, "playlist.rename": _ONE_PLAYLIST,
         "playlist.describe": _ONE_PLAYLIST,
         "download.album": "0 requests for the list if already opened, else 2; then each "
                           "track downloads paced 2 s apart",
         "download.playlist": "0 requests for the list if already opened, else 2; then each "
                              "track downloads paced 2 s apart",
         **{f"{v}.{k}": "1 request, queued, answers at once with position and ETA"
            for v in ("favorite", "unfavorite") for k in ("album", "artist", "playlist")}}


def _cost(name, spec) -> str:
    from ticli.agentq import WAITS
    if name in COSTS:
        return COSTS[name]
    if not spec.tidal:
        return "0 requests, at once"
    if spec.read:
        return "1+ requests, waits its turn, returns data"
    if name in WAITS:
        return "queued, waits its turn and returns the result"
    return "queued, answers at once with position and ETA"


def _row(name, spec) -> str:
    verb = LEGACY_VERBS.get(name, name.replace(".", " "))
    args = " ".join([*(f"[{p[:-1]}...]" if p.endswith("*") else f"[{p}]" for p in spec.params),
                     *(f"[{o}=]" for o in spec.options)])
    flags = " **DANGEROUS** (needs that switch)" if spec.dangerous else ""
    return (f"- `ticli agent {verb}{' ' + args if args else ''}` (`{name}`): {_cost(name, spec)}{flags}")


def render() -> str:
    from ticli.commands import COMMANDS
    rows = "\n".join(_row(name, spec) for name, spec in COMMANDS.items())
    return (HEAD + "\nEvery command, generated from the registry. Arguments in order;"
            " `[x...]` takes several.\n\n" + rows + "\n" + STATUS + LEGACY + TAIL)
