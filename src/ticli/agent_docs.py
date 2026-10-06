"""The text behind `ticli agent docs` — the complete contract for programs.

A plain module holding one string, so `ticli agent docs` costs an import of
nothing but this file and stays instant. The single source of truth for how
an agent uses ticli: README's Agents section and AGENTS.md point here rather
than restating it, and a test walks the click group to assert every verb that
exists is documented — the docs cannot silently fall behind the surface.

The one deliberate exception to the agent surface's JSON contract: this verb
prints markdown, because its consumer is a language model reading prose, not
a program parsing fields. The contract section below says so out loud.
"""

DOCS = """\
# ticli agent — the contract for programs

ticli is a terminal music player for TIDAL; the TUI is the human's surface.
`ticli agent` is yours: headless verbs that speak JSON, rate-limited in code.
Everything you can do with ticli programmatically is on this page. If you are
here to work on ticli's *source*, this is the wrong document — read
`CLAUDE.md` in the repo first.

## The contract

- Every verb prints **exactly one JSON object** on stdout, then exits.
  Success: `{"ok": true, ...}`. Failure: `{"ok": false, "error": <code>,
  "message": ..., "hint": ...}` with a nonzero exit code. stderr is for
  humans; parse stdout only. (`docs` — this page — is the one exception:
  it prints markdown, for you to read rather than parse.)
- Error codes you must handle: `not_logged_in`, `rate_limited`,
  `auth_failed`, `not_found` (a bad or stale id), `api_error` (anything
  else), and the permission refusals below (`ai_control_off`,
  `dangerous_off`, `key_required`, `wrong_key`). Each carries a `hint`
  saying what to do.
- Every verb's `--help` states its request cost. Budget before you loop:
  requests are spaced ~2 seconds apart by a throttle you cannot bypass, so
  20 resolves is ~40 seconds by design. Prefer verbs that answer in one
  request over loops that ask in many.

## The trip — read this before your first request

TIDAL rate-limits by IP, and a block stops the *owner's music*, not just
your call. The throttle spaces requests automatically; you never wait or
pace by hand. But if TIDAL answers 429 or flags the session, the surface
**trips**: every agent request from every process fails fast with
`"error": "rate_limited"` until a human clears it.

When you see `rate_limited`: **stop and report to the human.** That is the
entire procedure. Clearing the trip is `ticli agent unblock`, and it is the
human's command to run after investigating — a trip cleared on a guess gets
the IP banned for longer. Tripped mid-task: report what completed and what
was still pending from your own tally of the responses you already have —
spend nothing trying to reconcile, and leave finishing for after the human
clears it.

## Permissions — your human decides, you ask

Three switches in ticli's TUI settings decide what you may do. **Only the
human can change them**, by keypress in the TUI. No command or verb can
change them, and you must never edit `config.json`, write ticli's files or
pretend to be the TUI to get around them. Any change shows up in the TUI.

- **Allow AI control** (on by default). Off: every verb that reaches TIDAL
  is refused with `ai_control_off`; `status` still answers, and
  `playlist list` answers from what is saved on disk (`"source": "disk"`).
- **Allow dangerous commands** (off by default). Dangerous: deleting or
  renaming a playlist, removing tracks from one, deleting downloads,
  clearing the cache, lowering the cache budget, logging out, changing the
  login flow. Refused with `dangerous_off`.
- **AI control key** (unset by default). Set: every verb except `status`
  needs it, via `TICLI_AI_KEY` or `ticli agent --key KEY <verb>`. Missing →
  `key_required`; wrong → `wrong_key`, after a 1 s delay. Do not guess.

`status` reports all three as `ai_control`. On any refusal: stop and ask
your human, quoting the refusal's `hint`. That is the whole procedure.

## Verbs

### `ticli agent docs` — 0 requests
This page. The one verb that prints markdown instead of JSON, because its
consumer is you, reading — not a program, parsing.

### `ticli agent status` — 0 requests
Where everything stands, for free. Run it first when unsure of the setup.
```json
{"ok": true, "ai_control": {"allow_ai_control": true,
 "allow_dangerous_commands": false, "key_required": false},
 "session_stored": true, "flow": "pkce", "flac_capable": true,
 "player_running": true,
 "throttle": {"min_interval_seconds": 2.0, "tripped": null}}
```
`flow` is `"pkce"` (entitled to FLAC), `"device"` (AAC only — TIDAL
silently downgrades lossless requests from it), or `null` when no session
is stored (then `flac_capable` is `false` and stays honest). `player_running`
means the human's TUI holds the instance lock right now. `--verify` spends
1 request confirming the session actually works and adds `"verified":
true|false` to the output — it proves the login, not the audio: what codec
a live stream actually granted is only observable in the TUI's quality
badge, so answer entitlement questions from `flow`/`flac_capable` and say
that distinction if the human asks about the bytes.

### `ticli agent resolve --artist A --title T [--limit N]` — 1 request
**The verb for "the user named a song."** Searches once, ranks locally
(`--limit` candidates, default 10), answers with a best pick and a strict
`confident` flag.
```json
{"ok": true, "artist": "Folamour", "title": "The Journey", "confident": true,
 "best": {"id": 176427254, "title": "The Journey (feat. Zeke Manyika)",
          "artists": ["Folamour", "Zeke Manyika"], "album": "The Journey",
          "duration_seconds": 262, "explicit": false,
          "artist_match": true, "title_exact": true,
          "unrequested_qualifier": false, "score": 2},
 "candidates": [ ...same shape, ranked... ]}
```
The ranking rules, so you can trust them: artist is a **gate** — a
wrong-artist candidate never outranks a right-artist one, however exact its
title. Remixes/edits/live versions you did not ask for are demoted and
labeled (`unrequested_qualifier`), but still returned when they are all
there is. `feat.` credits are ignored in matching — a featured guest is the
same recording. `confident` means: right artist, exact title, no
unrequested qualifier. Anything less is your judgement call, and the ranked
`candidates` list is there for you to make it — or to put to the human.

### `ticli agent search QUERY [--type track|album|artist|playlist] [--limit N]` — 1 request
Raw search when resolve's artist+title shape doesn't fit (browsing, albums,
finding a playlist on TIDAL). `--type` repeats; one request however many
types. Default: tracks, limit 10. Output: `{"ok": true, "query": "...",
"tracks": [...], "albums": [...]}` — only the types you asked for. Tracks
are resolve's shape minus the ranking fields; albums are `{id, title,
artists, num_tracks, year}`; artists `{id, name}`; playlists `{id, name,
num_tracks, description}`.

### `ticli agent playlist list` — 1 request
All of the user's playlists: `{"ok": true, "playlists": [{"id": "...",
"name": "...", "num_tracks": 14, "description": ""}]}`. To find one by
name, list and match locally — case-insensitive; if more than one matches,
ask the human rather than guessing.

### `ticli agent playlist show ID` — 2 requests
One playlist and its tracks: `{"ok": true, "playlist": {...},
"tracks": [...]}`.

### `ticli agent playlist create NAME [--description D]` — 1 request
Creates empty; answers `{"ok": true, "playlist": {"id": ...}}`. The id is
what `add` needs — capture it.

### `ticli agent playlist add ID TRACK_ID...` — 2 requests
However many ids, one add — **batch them, never add in a loop.** The server
skips duplicates; `{"ok": true, "playlist_id": "...", "requested": 5,
"added": 5}` tells you what it actually took.

There is no `playlist delete`, no track removal, no destructive verb of any
kind in this surface yet; when they come they will need the human's "Allow
dangerous commands". Asked to delete a playlist or remove tracks: report
that it is done in the TUI (`x` on the track, with a confirmation). Do not
go looking for another way.

### `ticli agent unblock` — 0 requests
Clears a trip. **Human-only** — see The trip above. Present it to the user
as the command *they* run; do not run it for them.

## Workflows

**The user names songs; you build a playlist.** resolve each song →
`playlist create` → one `playlist add` with every id. Done means: every
track you added came from a `confident` resolve or an explicit human choice
from `candidates`; anything else you report by name, with what you picked
instead or why you skipped it. N songs is N+2 requests ≈ 2(N+2) seconds —
say so if the list is long.

**"Add this to my X playlist."** `playlist list` → match X locally →
resolve the song → `add`. Ambiguous name or non-confident resolve: ask,
with the candidates. No playlist matches X at all: ask before creating
one — never auto-create a playlist the user referred to as existing.

**"What's my setup / can I get FLAC?"** `status`, free. `flac_capable`
false → the fix is the human running `ticli` and pressing `u` on the
settings page (PKCE sign-in).

**Anything that touches playback** — play, pause, skip, queue, volume,
what's playing, downloads, favorites, radio: **not in this surface yet.**
Say it is not yet supported and point at the TUI; the running player is the
human's. (Control of the live player is planned, not built yet.) The one
thing you *can* see
is `status`'s `player_running` boolean.

Also not here yet: **browsing** (an album's track list, an artist's page —
`search` returns albums and artists but nothing opens them) and **settings**
(quality, cache, artwork — TUI-only). Same procedure: say so, point at the
TUI.

## Ground rules

- This surface is the only sanctioned path. Importing ticli's internals or
  calling TIDAL's API directly bypasses the throttle and is how the owner's
  IP got blocked; if a task seems to need it, the task is out of scope —
  report that instead.
- Playlists you create are real TIDAL playlists, visible in the TUI
  (`p`) and every TIDAL app, immediately.
- The TUI and this surface share one login. Never touch the credential
  store; `not_logged_in` means the human runs `ticli`, nothing else.
"""
