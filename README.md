# Ticli

# Note to self: I should really update this README











An unofficial terminal music player for TIDAL. Search, browse, queue, and play music — all from your terminal. Not affiliated with TIDAL.

Ticli connects directly to TIDAL's API using your premium account. No desktop app needed. Just authenticate, search, and play.

Works on **macOS** and **Linux**.

```
╭──────────────────────── Ticli ────────────────────────╮
│                                                        │
│  ▶ ♥ Arlo Parks - Sophie                               │
│     Super Sad Generation                               │
│     1:47 ━━━━━━━━●━━━━━━━━━━━━━━━━━━━━━━━━━━━ 3:28    │
│     Queue: 3/12  LOSSLESS                              │
│     Next: Cola • Arlo Parks                            │
│                                                        │
│  [space] play/pause  [n/→] next  [←] prev             │
│  [s] search  [q] queue  [p] playlists                  │
│  [l] like  [r] radio  [t] mini  [m] more               │
│                                                        │
╰────────────────────────────────────────────────────────╯
```

## Features

- **Search** — Find tracks, albums, artists, and playlists
- **Browse** — Navigate album and playlist tracklists
- **Queue** — Manage your playback queue, reorder, remove tracks
- **Playlists** — Browse and play your saved playlists
- **Likes** — Toggle favorites on any track
- **Radio** — Generate a station from any track
- **Mini mode** — Condensed single-line display
- **Session restore** — Picks up where you left off
- **Lossless & Hi-Res** — Stream up to 24-bit/192kHz FLAC
- **Secure auth** — OAuth tokens stored in your OS keychain

## Install

Requires Python 3.10+ and [ffmpeg](https://ffmpeg.org).

```bash
# macOS
brew install ffmpeg python3
pip install tidal-cli

# Ubuntu / Debian
sudo apt install ffmpeg python3-pip
pip install tidal-cli
```

For secure token storage in your OS keychain (recommended):

```bash
pip install "tidal-cli[keyring]"
```

## Usage

```bash
ticli
```

On first run you'll get a URL to authorize with your TIDAL account. After that, your session is cached and you go straight to the player.

### Quality

```bash
ticli --quality HIRES      # 24-bit hi-res FLAC
ticli --quality LOSSLESS   # 16-bit FLAC
ticli --quality HIGH       # lossless FLAC (default)
ticli --quality LOW        # 320kbps
```

### Keybindings

#### Player

| Key | Action |
|-----|--------|
| `space` | Play / pause |
| `n` `→` | Next track |
| `←` | Previous track |
| `s` | Search |
| `v` | Volume (`←` `→` to adjust, `Enter` or `Esc` to close) |
| `q` | Queue |
| `p` | Playlists |
| `l` | Like / unlike track |
| `r` | Start radio from track |
| `t` | Toggle mini player |
| `m` | Show more controls |
| `esc` | Quit |

On macOS (with mpv installed) the system media keys, AirPods taps and Control Center
also drive play/pause, next and previous, and the current track shows up in Now Playing.

#### Search

| Key | Action |
|-----|--------|
| `↑` `↓` | Navigate results |
| `enter` `→` | Play track / open album or artist |
| `backspace` | Delete character |
| `esc` `←` | Back |

#### Queue

| Key | Action |
|-----|--------|
| `↑` `↓` | Navigate |
| `enter` | Jump to track |
| `x` | Remove track |
| `esc` `←` | Back |

## How it works

Ticli uses [tidalapi](https://github.com/tamland/python-tidal) to authenticate and fetch audio stream URLs. Audio is played through [ffplay](https://ffmpeg.org/ffplay.html). The TUI is built with [Rich](https://github.com/Textualize/rich).

```
┌─────────┐     OAuth      ┌───────────┐    stream URL    ┌───────────┐
│  Ticli  │ ──────────────► │  TIDAL    │ ──────────────►  │  ffplay   │
│  (TUI)  │ ◄────────────── │  API      │                  │           │
└─────────┘    metadata     └───────────┘                  └───────────┘
```

## Contributing, and a note for AI agents

**Start with [`ai/README.md`](ai/README.md).** Much of this project was built
with AI assistance, and `ai/` is where the reasoning lives: the constraints that
are load-bearing, what was measured, what was tried and rejected, and the
incidents that produced the rules. It is short, and reading it first will save
you from re-deriving things the hard way — several of its rules exist because
violating them broke something real.

Two constraints are worth knowing before you touch anything:

- **Be frugal with TIDAL's API.** Rate-limiting an account is easy to do by
  accident and it stops the owner's music. `ai/WORKING-RULES.md` has the
  specifics.
- **Tests assert observable reality** — bytes on disk, escape sequences on the
  terminal, request counts — not internal bookkeeping. A feature here once
  passed its whole suite for two days without writing a single byte.

If you are an AI agent working on this repository, **updating `ai/` is part of
the work, in the same commit as the code.** `ai/README.md` says what belongs
where.

## Requirements

- macOS or Linux
- Python 3.10+
- TIDAL Premium subscription
- ffmpeg

## Credits

Created and maintained by [odonald](https://github.com/odonald).

Contributors:

- [Garrett Simko](https://github.com/Starwaves1) — lossless/hi-res playback (PKCE login, segmented
  DASH streams), album artwork, metadata and audio caching, scrubbing, scoped search, the artist
  page, and macOS media keys.

## Support

If you enjoy Ticli, consider [buying me a coffee](https://buymeacoffee.com/odonald).

## License

MIT
