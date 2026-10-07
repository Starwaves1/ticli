# Ticli

A terminal music player for TIDAL: streams, caches and downloads audio through tidalapi, played by mpv or ffplay.

## Quality

**Quality tier**:
One of `LOW` (96k AAC), `MEDIUM` (320k AAC), `HIGH` (16/44.1 FLAC) or `MAX` (hi-res FLAC), named the way TIDAL's own app names them. This is what the user picks and what the UI shows.
_Avoid_: `LOSSLESS`, `HIRES` (legacy CLI aliases only)

**Wire value**:
tidalapi's `Quality` spelling (`LOW`, `HIGH`, `LOSSLESS`, `HI_RES_LOSSLESS`), sent to TIDAL and used in every persisted comparison. `HIGH` means different things on each side: ticli `HIGH` is wire `LOSSLESS`, and wire `HIGH` is ticli `MEDIUM`.
_Avoid_: mixing the two spellings in one variable

**Granted tier**:
The tier TIDAL actually served for a stream request, in wire spelling. The **ceiling** is the highest tier this login has been observed to get, learned only from downgrades; tiers above it are shown dimmed with the reason.
_Avoid_: "quality" on its own when the asked and granted tiers could differ

**Device flow / PKCE flow**:
The two TIDAL logins. The device flow (default) is a code typed on another device and is only ever served AAC; the PKCE flow is a browser sign-in with a pasted-back redirect and is the only login TIDAL streams FLAC to.
_Avoid_: "OAuth login" (both are OAuth), "hi-res login"

**BTS stream / segmented stream**:
The two stream shapes TIDAL returns. A BTS stream is one URL (device-flow AAC); a segmented stream is an MPEG-DASH manifest of an init segment plus fMP4 segments (PKCE FLAC), which ticli rewrites as a local HLS playlist.
_Avoid_: "DASH stream" for the HLS playlist ticli writes

## Storage

**Cache**:
Machine-owned, disposable audio under the OS cache directory, held to `cache_budget_gb` and evicted by value.
_Avoid_: downloads, library, offline copies

**Download**:
A user-owned, tagged file in `~/Music/Ticli/<Artist>/<Album>/`. Outside the budget and eviction, and never re-fetched without a human's `[R]` or an agent's `refetch` with dangerous commands allowed.
_Avoid_: cached song, saved song

**Scratch copy**:
A local copy of the playing track kept only so it can resume, deleted on stop. Not part of the cache.

**Local copy**:
A file in either the cache or downloads for a track id, verified on disk at the moment of use (`_local_source`).
_Avoid_: "downloaded" when either tier is meant

**Cache tracker**:
`audio.json` in the cache directory: the per-track record of cached audio (extension, granted tier, size, plays, last played). It decides what the cache should hold; the disk decides what exists, and **reconcile** makes the two agree.
_Avoid_: "index", which means the **metadata index** or the **download index** (`downloads.json`)

**Metadata index**:
`lists/` in the cache directory, one file per list plus a manifest: every list the user has opened (playlists, album tracks, artist sections, searches, favourites, mixes), never expiring, held to its own 100 MB cap (ADR-0009). A first paint while online; the answer, with its age, while offline.

**Connectivity**:
The player's online / offline / signed_out state, pushed to every client. Offline means TIDAL is unreachable; signed_out means it rejected the stored login and a human must sign in again with [o]. Only an action that needs TIDAL tries to reconnect.

**Play**:
A listen that has passed 30 s, or half of a track shorter than a minute. A skip is not a play.
_Avoid_: listen, hit

**Value**:
A cached track's `(plays, last played)`. The lowest value is evicted first and refused admission first: fewest plays, oldest among ties.
_Avoid_: score, LRU rank

**Part file**:
`{track_id}.part`, bare with no extension: audio still arriving, renamed to `{track_id}{ext}` when whole.
_Avoid_: temp file

**CachedTrack**:
A track rebuilt from a stored record with plain fields only. It becomes a real tidalapi `Track` only through `_resolve_track`, which is a network call.
_Avoid_: "cached track" for a tidalapi `Track` that happens to have a local copy

## Interface

**Scope**:
Which result category search shows: All, Tracks, Albums, Artists, Playlists (TIDAL's) or My Playlists (yours, answered locally with no request). One search fetches every category, so changing scope is free. The internal key `playlists` is My Playlists; `tidal_playlists` is TIDAL's.
_Avoid_: filter (in prose)

**Player focus**:
The state where `↑` has handed the arrow keys to the progress bar, so `←`/`→` seek instead of navigating; `↓`, Esc or any other key drops it.
_Avoid_: seek mode, scrub mode

**Player (process)**:
The background `ticli.playerd` that owns audio, the TIDAL session, queue, saved state, downloads and caches. Every TUI and CLI call is a client of its socket; it outlives the terminal and leaves when idle and alone.
_Avoid_: daemon, server (in prose)

**Handover**:
Replacing a running player with one on newer code, keeping its place: the new player asks whether the old one plays, SIGTERMs it (which saves queue, index and position on every build) and resumes there. Clients start one on their own when the player's **code fingerprint** is older and it runs no job; `ticli restart` starts one on demand.
_Avoid_: reload, upgrade (in prose)

**Code fingerprint**:
A hash of the package's `.py` paths, mtimes and sizes plus the newest mtime, taken when a process imports `ticli.ipc` and exchanged in `hello`. Only a client with the newer one replaces a player.
_Avoid_: version (the package version doesn't change on an edit)

**Agent surface**:
`ticli agent <verb>`, the JSON-only CLI for programs. `HeadlessTidalPlayer` is the player core, and the TUI when attached with `remote`, despite its name.
_Avoid_: API, headless mode

**Command**:
One named player-level action in `commands.py` (`next`, `playlist.add`, `cache.clear`...), taking and returning JSON-shaped data. TUI keys, `ticli <verb>` and `ticli agent <verb>` all call the same commands; screens, cursors and menus are not commands.
_Avoid_: action, endpoint

**Caller**:
Who a command runs for: `human` (TUI keys, `ticli <verb>` from a terminal) or `agent` (`ticli agent`, or `ticli <verb>` without a TTY). Only agent calls are gated.

**AI control**:
The human's three TUI-only switches: Allow AI control, Allow dangerous commands, and the AI control key. No command can change them (ADR-0007).

**Dangerous command**:
A command that deletes or is hard to undo: removing from, deleting, renaming or re-describing a playlist, deleting a download, re-fetching the library (hundreds of requests), clearing the cache, lowering the cache budget, logout, changing the login flow. Refused to agents unless Allow dangerous commands is on.

**Agent notice**:
The transient `agent: ...` status line a connected TUI shows after an agent's action ("agent: queued 3 tracks"), so the human watches what the AI did. Reads show none.
_Avoid_: agent toast, AI log

**Trip**:
The throttle's persisted stop, written on a 429 or 401/4006. Every agent request fails fast until a human runs `ticli agent unblock`.
_Avoid_: cooldown, backoff (it never expires on its own)

**Confident**:
The strict outcome of `ticli agent resolve`: artist matched, normalized title equal, no unrequested qualifiers. Anything less returns the ranked candidates instead of choosing.
_Avoid_: best match, top hit
