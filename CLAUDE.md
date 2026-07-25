# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Ticli: a terminal music player for TIDAL. Streams lossless/hi-res audio directly from TIDAL's API via tidalapi + ffplay.

## Repository Layout

- `src/` — Python package (`ticli`)
  - `ticli/player.py` — Main player (TUI, audio, search, queue, playlists)
  - `ticli/cli.py` — Click CLI entry point
  - `ticli/utils/credential_store.py` — Secure OAuth token storage
  - `ticli/utils/cache.py` — On-disk metadata/audio cache + budget
  - `ticli/tests/` — E2E tests

## Commands

```bash
# Activate the Python environment
source ./src/.venv/bin/activate

# Install the package (editable)
cd src && pip install -e ".[keyring]"

# Run tests
pytest ticli/tests/ -v

# Launch the player
ticli
ticli --quality HIRES
```

## Architecture

Ticli uses `tidalapi` (community Python client) to authenticate via OAuth and fetch audio stream URLs. Audio is played through ffplay (from ffmpeg). The TUI is built with Rich's `Live` display.

### Audio Playback

- ffplay: kills process on pause (instant silence), restarts from the downloaded local copy on resume
- mpv (if available): uses IPC socket for pause/resume
- macOS media keys (mpv only): mpv registers with MPRemoteCommandCenter, so keyboard
  media keys / AirPods taps / Control Center reach it. Ticli rebinds those keys over
  IPC (`keybind`) to write `user-data/ticli/media-key`, which `_monitor_playback` polls
  on its existing 0.5s tick. Gated on `IS_MACOS`; a silent no-op elsewhere and on ffplay.

### Login flows and quality entitlement

Two OAuth flows, and the difference is audible. The **device flow**
(`login_oauth`, the default) is the smooth one — a code on your phone,
nothing to paste back — but its TIDAL client is only entitled to AAC:
it accepts a `LOSSLESS`/`HI_RES_LOSSLESS` request and grants `HIGH`,
silently, with a byte-identical manifest. The **PKCE flow**
(`pkce_login_url` → `pkce_get_auth_token` → `process_auth_token`, driven
step by step rather than through tidalapi's `login_pkce()`, which
`print()`s and `input()`s) uses the client tidalapi documents as "the
only way how to get access to HiRes … FLAC files". It is opt-in: `[u]`
on the settings page, or `--login-flow pkce` on a first run. The paste
is unavoidable — the redirect URI is fixed to a tidal.com page in
tidalapi's config and re-sent in the token exchange, so no localhost
listener can stand in — and it has to work over SSH anyway, so the
prompt also accepts a bare code. The TUI stands down for the duration
(`_suspended_tui`): the one place the Live display is deliberately
paused.

Stored tokens record **which flow issued them** (`is_pkce`), because
`Session.token_refresh` picks the client id/secret from that flag alone.
A record that lost it refreshes against the wrong client and the session
dies hours later, looking like a random logout. Records predating the
flag are device-flow by construction, so migration is a defaulted read —
nothing on disk is rewritten and nobody is logged out.

Quality gating is evidence-based and costs no requests: `_stream_url`
asks for the whole stream description (one request either way, and
`get_url()` raises on a PKCE session), and `_note_granted_quality`
remembers only a *downgrade* — being granted what you asked for says
nothing about the tiers above it. Gated tiers stay listed and dimmed
with the reason rather than hidden; when nothing has been observed,
nothing is gated. A successful PKCE upgrade clears the ceiling, so the
tiers re-open without a restart. Songs already cached keep their old
quality — the upgrade toast says so and points at `[x]`.

### Caching

`utils/cache.py` holds a metadata index (your playlists, and the tracks in
each) in the OS's own cache directory — never in `~/.config/ticli`. Lists
paint from it instantly, but it never answers alone: every read is paired
with the live fetch, which replaces what was shown one round trip later.
Cached rows are `CachedTrack` / `CachedPlaylist` records, not tidalapi
objects; anything that needs the real thing resolves through the session
first. Two independent settings gate it — `cache_metadata` (the index) and
`cache_songs` (whole tracks) — and `cache_budget_gb` sizes it in whole
gigabytes; eviction runs after writes, never on a timer. Disabling and
clearing are separate: turning `cache_songs` off asks `Clear cached
songs as well?` (y clear, n keep them, Esc cancel) and touches neither
the setting nor the disk until that is answered. Clearing is also its
own action — `[x]` on the settings page, a keybinding outside
`SETTINGS_SPEC` for the same reason logout is, with its own
confirmation. A clear really clears: files in use are unlinked too. On
POSIX the playing process keeps its descriptor and plays on (verified
with mpv); if it had not opened the file yet it exits at once and
`AudioPlayer.source_vanished` + `_monitor_playback` restart the track
from the network where it left off (`_track_has_time_left` keeps that
from firing on a track that had already finished). On Windows a file
open without delete-sharing cannot be unlinked; those are counted and
reported in the toast rather than passed over silently. Deletion and
eviction only ever unlink files ticli itself wrote (`is_owned_audio` /
`owned_audio_files`: `{track_id}{ext}` and `.part`) — never the
directory — so anything else living there survives.

`cache_songs` also keeps the audio. TIDAL serves a track as one contiguous,
unencrypted HTTP file, so `AudioPlayer._start_download` fetches it with a
plain `requests` GET on a daemon thread while the player streams the same
URL — no ffmpeg, and identical on mpv and ffplay because the download no
longer rides on the player process. The file is named `{track_id}{ext}`
where the extension comes from the CDN's `Content-Type` (AAC-in-MP4 today,
FLAC for a session entitled to it), written as `.part` and renamed only
when whole, so a lookup by stem can never serve a partial file. A `stop()`
bumps a generation counter, which is how an abandoned download knows to
delete itself.

### Key Files

| File | Purpose |
|------|---------|
| `player.py` | Player TUI, audio control, search, queue, playlists (~1400 LOC) |
| `cli.py` | CLI entry point |
| `utils/credential_store.py` | OAuth token storage (keychain + fallback) |
| `utils/cache.py` | Metadata cache, cached audio, budget + eviction |

## Testing

Tests use Click's `CliRunner` and subprocess calls to verify CLI help text and argument parsing. No running TIDAL instance needed.
