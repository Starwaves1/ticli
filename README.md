# Ticli

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

## Requirements

- macOS or Linux
- Python 3.10+
- TIDAL Premium subscription
- ffmpeg

## Support

If you enjoy Ticli, consider [buying me a coffee](https://buymeacoffee.com/odonald).

## License

MIT
