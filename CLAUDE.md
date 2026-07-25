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

- ffplay: kills process on pause (instant silence), caches audio to local temp file, restarts from cache on resume
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
first. The `cache_mode` / `cache_budget_mb` settings size it, and eviction
runs after writes — never on a timer.

### Key Files

| File | Purpose |
|------|---------|
| `player.py` | Player TUI, audio control, search, queue, playlists (~1400 LOC) |
| `cli.py` | CLI entry point |
| `utils/credential_store.py` | OAuth token storage (keychain + fallback) |
| `utils/cache.py` | Metadata cache, cached audio, budget + eviction |

## Testing

Tests use Click's `CliRunner` and subprocess calls to verify CLI help text and argument parsing. No running TIDAL instance needed.
