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
