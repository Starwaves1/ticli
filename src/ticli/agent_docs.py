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
 "state": {"track": {"id": 1, "title": "T", "artist": "A", "pos": 12, "dur": 200},
           "playing": true, "queue": {"len": 3, "index": 0},
           "switches": {"ai": true, "dangerous": false}, "pending": 2},
 "next": ["pause", "next", "queue list"],
 "cost": {"requests": 1, "wait_s": 4.0, "eta_s": 10.0}}
```
- `state` is a snapshot after the call (`track` is null when nothing is loaded;
  `pending` appears only while agent commands wait). `next` is up to 5 verbs
  that apply right now. `cost`: TIDAL `requests` used, `wait_s` you waited in
  the queue, `eta_s` until the last queued command finishes. Track durations
  are `duration_seconds` everywhere (`dur` in `state`).
- Failure: `{"ok": false, "code", "reason", "fix"}` and exit 1. Codes:
  `ai_control_off`, `dangerous_off`, `key_required`, `wrong_key` (permissions),
  `rate_limited`, `not_logged_in`, `auth_failed`, `not_found` (stale or wrong
  id), `stale` (the queue moved; the reply carries the current `queue`),
  `bad_args`, `empty`, `no_track`, `human_only`, `api_error`,
  `player_unavailable`. Act on `fix`. The original verbs (`search`, `resolve`,
  `playlist ...`) also keep `error`, `message`, `hint`, and their own result keys.
- Arguments are positional in the order shown below, `key=value`, or one JSON
  object. Ids are what verbs take: get them from `search`, `resolve`, `playlist list`.

## The queue: pacing, ETAs, coalescing

Every verb that reaches TIDAL waits in one queue in the player, shared by all
agents, **requests 2 s apart**; you never pace by hand and cannot bypass it.
- **Reads** (`search`, `resolve`, `playlist list/show`, `album tracks`,
  `artist section`) block until answered and return the data; `cost.wait_s` is
  what the queue made them wait.
- **Actions** (play, like, playlist add, download...) answer at once with
  `result.queued` (position) and `result.job`; `cost.eta_s` is when the last
  one should finish. `ticli agent status` lists `pending` and recent `done`.
- **Local** commands (pause, queue edits, settings reads) cost 0 requests and
  run at once.
- **Coalescing**: waiting adds to one playlist merge into one add (up to 100 ids
  per request, plus TIDAL's re-read: 2 requests for 1-100 ids); waiting likes
  merge too. The reply says so in `result.merged`:
  batch them, never add in a loop.
- **`ticli agent do`** runs a JSON array in order with one combined reply, local
  commands first, TIDAL ones queued together so they coalesce:
  `ticli agent do '["playlist add ID 1 2", "like 3", "queue list"]'` (or the
  array on stdin; items may be `{"cmd": "playlist.add", "args": {...}}`). A
  failing item skips the rest (`"code": "skipped"`).
- Queue entries: pass the `track_id` you saw with the index
  (`queue remove 2 TRACK_ID`); if the queue moved you get `stale`, not another track.

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
  `settings get`, `playlist list`, `search` over your own playlists) answer from
  disk, 0 requests, `"source": "disk"`, and never start the player.
- **Allow dangerous commands** (off by default): deleting playlists or
  downloads, removing playlist tracks, clearing the cache, lowering the cache
  budget, logout, changing the login flow. Refused with `dangerous_off`.
- **AI control key** (unset by default). Set: every verb except `status` needs
  it via `TICLI_AI_KEY` or `ticli agent --key KEY <verb>`; wrong keys cost 1 s.
On any refusal quote its `fix` to your human and stop.

`ticli <verb>` (no `agent`) without a terminal on stdin is treated as you too:
same switches and key, readable text output, and it accepts names and songs
(a playlist name from your own playlists, a TIDAL URL, `"artist - title"`,
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

**"Add this to my X playlist."** `playlist list`, match X case-insensitively;
more than one match or no match: ask. Ask first; never auto-create a playlist the user called existing.

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

### `ticli agent search QUERY [--type track|album|artist|playlist] [--limit N]` — 1 request
One request however many `--type`. Output `{"query", "tracks": [...], ...}`: tracks as
above minus ranking; albums `{id, title, artists, num_tracks, year}`; artists
`{id, name}`; playlists `{id, name, num_tracks, description}`.

### `ticli agent playlist list|show|create|add`
`playlist list` (1 request) `{"playlists": [...]}`; `playlist show ID` (2)
`{"playlist", "tracks"}`; `playlist create NAME [--description D]` (1) answers
`{"playlist": {"id"}}`, capture the id; `playlist add ID TRACK_ID...` is queued
and coalesced (2 requests per 100 ids); the reply has `requested`, `queued`, no
`added` (not known yet): check with `playlist show`.

### `ticli agent docs`, `ticli agent do`, `ticli agent unblock`
`docs` prints this page (0 requests, markdown). `do` is described above.
`unblock` is Human-only.
"""


def _cost(spec) -> str:
    if not spec.tidal:
        return "0 requests, at once"
    return "1+ requests, waits its turn, returns data" if spec.read \
        else "queued, answers at once with position and ETA"


def _row(name, spec) -> str:
    verb = LEGACY_VERBS.get(name, name.replace(".", " "))
    args = " ".join(f"[{p[:-1]}...]" if p.endswith("*") else f"[{p}]" for p in spec.params)
    flags = " **DANGEROUS** (needs that switch)" if spec.dangerous else ""
    return (f"- `ticli agent {verb}{' ' + args if args else ''}` (`{name}`): {_cost(spec)}{flags}")


def render() -> str:
    from ticli.commands import COMMANDS
    rows = "\n".join(_row(name, spec) for name, spec in COMMANDS.items())
    return (HEAD + "\nEvery command, generated from the registry. Arguments in order;"
            " `[x...]` takes several.\n\n" + rows + "\n" + STATUS + LEGACY + TAIL)
